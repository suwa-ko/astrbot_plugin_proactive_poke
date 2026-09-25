"""astrbot_plugin_proactive_poke

让机器人**主动戳一戳**别人。三种触发场景，各自独立开关与概率：

1. **回复前戳**（``poke_before_reply``）：机器人准备回复某人之前，按概率先戳他一下。
   用时间窗界定「一轮完整对话」——戳过一次后，同一会话在 ``round_window`` 秒内不会再戳。
2. **不回复时戳**（``poke_on_silent``）：群里有人发言但**没有触发机器人回复**时，用更低的概率戳他。
3. **机器人自己决定**（``poke_by_llm``）：给 LLM 注册一个 ``poke`` 工具，它可以自己判断
   「这里戳一下比发消息更合适」，也可以在对话里 @ 到某人时戳那个人。

适配 AstrBot 的 aiocqhttp(OneBot v11) 适配器 —— 即 NapCat / LLOneBot / Lagrange.Core 等 QQ 协议端。

实现要点（以下判断都经过真实框架实测，不是猜的）：

- **场景 1 挂在 ``@filter.on_decorating_result()``**：这个钩子由 ``ResultDecorateStage`` 触发，
  而 ``STAGES_ORDER`` 里它在 ``RespondStage`` 之前，所以确实是「回复发出去之前」。
- **场景 2 需要一条「所有群消息」的 handler**。注册这种 handler 会把事件标记为 ``is_wake = True``
  （``WakingCheckStage`` 里「插件 handler filter 通过」也是一条唤醒条件），
  但它**不会**设置 ``is_at_or_wake_command``；而 ``ProcessStage`` 的默认 LLM 请求要求
  ``is_at_or_wake_command`` 为真。实测：群里普通聊天时我们的 handler 跑 1 次、LLM 调用 0 次。
  所以插件用这个标志区分「消息本身唤醒了机器人」和「只是我们的 handler 让它醒过来」。
- **发送**只能走 OneBot 接口：群聊 ``group_poke``、私聊 ``friend_poke``，失败再试 ``send_poke``。
  三者参数都是 ``user_id`` 必填、带 ``group_id`` 即群戳。
  ⚠️ **poke 消息段不能用来发送**：NapCat 源码里它的发送转换器就是 ``async () => undefined``，
  消息段会被静默丢弃，只发 poke 段还会直接报「消息体无法解析」。所以不做自动降级。
- **别人戳机器人时**，AstrBot 会把它当成一条只含 ``Poke`` 组件的消息投进来。
  私聊里这种消息会唤醒机器人、触发 LLM 回复，进而触发场景一 —— 不拦住就会演变成互戳，
  所以两个自动场景都显式跳过这种事件。
- 戳一戳依赖 NapCat 的 **packetBackend**（OIDB 0xED3 原生发包），受 QQ 版本白名单限制；
  不可用时接口报「packetBackend 发包能力不可用」，插件会把它翻译成一句人话。
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Poke
from astrbot.api.star import Context, Star

try:  # AstrBot >= 4.x 的推荐写法
    from astrbot.api import AstrBotConfig
except ImportError:  # pragma: no cover - 兼容更早的版本
    from astrbot.core import AstrBotConfig


# ---------------------------------------------------------------- 常量与工具

POKE_SEGMENT_TYPE = "126"
"""poke 消息段的 type 值，对应「戳一戳」。见 AstrBot 的 ``Comp.Poke``。"""

MAX_TRACKED_SESSIONS = 500
"""最多同时跟踪多少个会话的状态，超出后淘汰最久未活动的。"""

SESSION_TTL_SECONDS = 24 * 3600
"""会话状态多久没活动就丢弃。"""

RATE_WINDOW_SECONDS = 3600
"""``limits.max_per_hour`` 的滑动窗口长度。"""

SEND_TIMEOUT_SECONDS = 5.0
"""单次戳一戳调用的超时上限，避免拖慢正在发送的回复。"""

CONFIRM_TIMEOUT_SECONDS = 3.0
"""等待协议端回传「戳一戳已发出」事件的秒数。

NapCat 的 poke 接口只要包发出去了就返回成功，**成功不等于对方收到**
（对方屏蔽、非好友、服务端丢弃都可能）。唯一可靠的反馈是协议端随后上报的 poke 事件，
而且机器人自己发的戳也会上报，所以可以用它做送达确认。
"""


def _as_int(value: Any) -> Any:
    """尽量把 QQ 号 / 群号转成 int，OneBot 接口普遍更偏好 int。"""
    text = str(value).strip()
    if text.lstrip("-").isdigit():
        try:
            return int(text)
        except ValueError:
            return value
    return value


def _as_id_list(value: Any) -> list[str]:
    """把配置里的群号列表规整成字符串列表（兼容被手写成单个字符串的情况）。"""
    if value is None or value == "":
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _poke_segment(target: str) -> dict[str, Any]:
    """构造 poke 消息段（复用 AstrBot 自己的序列化，避免字段名猜错）。"""
    return Poke(id=target).toDict()


def _explain_poke_error(exc: BaseException | None) -> str:
    """把协议端的报错翻译成人话。"""
    if exc is None:
        return "没有可用的发送方式"
    raw = str(exc).strip() or exc.__class__.__name__
    low = raw.lower()
    if "packetbackend" in low or "packet backend" in low:
        return (
            "NapCat 的 packetBackend 发包能力不可用——戳一戳依赖它，"
            "通常是 QQ 版本不在支持范围内。请查看 NapCat 启动日志，"
            "并参考官方「高级配置」文档确认 packetBackend 状态。"
        )
    if "消息体无法解析" in raw:
        return "该协议端不支持用 poke 消息段发送戳一戳。"
    if "user_id" in low and ("expected" in low or "缺少" in raw):
        return "调用参数缺少 user_id（NapCat 的 poke 接口要求必填）。"
    return raw[:200]


@dataclass(frozen=True)
class PokeOutcome:
    """一次戳一戳的结果。

    注意区分三件事，它们不是一回事：

    - ``ok``：流程走通了（没被冷却/上限拦住，接口也没报错）
    - ``sent``：真的发出去了。``dry_run`` 模式下为 False —— **不能把 dry-run 当成成功**
    - ``confirmed``：协议端有没有回传「这个戳发出去了」的事件。
      ``None`` 表示没等确认，``True``/``False`` 是确认结果。
    """

    ok: bool
    detail: str
    sent: bool = True
    confirmed: bool | None = None


@dataclass
class SessionState:
    """单个会话（unified_msg_origin）的搓一搓状态。"""

    touched: float = field(default_factory=time.time)
    pokes: deque[float] = field(default_factory=lambda: deque(maxlen=200))
    """本会话最近的戳时间戳，用于「每小时上限」和「最小间隔」。"""

    last_before_reply: float = 0.0
    """场景一上一次戳的时间，用于界定「一轮对话」。"""

    last_on_silent: float = 0.0
    """场景二上一次戳的时间，用于群级冷却。"""

    last_by_llm: float = 0.0
    """场景三上一次戳的时间。"""

    user_last: dict[str, float] = field(default_factory=dict)
    """对每个人的最近一次戳，用于用户级冷却。"""


class ProactivePokePlugin(Star):
    """主动戳一戳。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self._sessions: dict[str, SessionState] = {}
        # 独立 Random 实例，方便测试里替换成确定性实现
        self._rng = random.Random()
        self._logged_send_method: str | None = None
        # 送达确认：目标 QQ -> 等待被协议端事件点亮的 Future
        self._pending_confirms: dict[str, asyncio.Future[bool]] = {}
        self._confirm_tasks: set[asyncio.Task[Any]] = set()

    # -------------------------------------------------------------- 配置读取

    def _opt(self, path: str, default: Any) -> Any:
        """读嵌套配置，例如 ``_opt("poke_before_reply.probability", 0.3)``。"""
        node: Any = self.config
        for part in path.split("."):
            if not isinstance(node, dict):
                return default
            node = node.get(part)
            if node is None:
                return default
        return default if node is None else node

    def _opt_bool(self, path: str, default: bool) -> bool:
        value = self._opt(path, default)
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)

    def _opt_int(self, path: str, default: int, *, low: int, high: int) -> int:
        try:
            value = int(self._opt(path, default))
        except (TypeError, ValueError):
            return default
        return min(max(value, low), high)

    def _opt_probability(self, path: str, default: float) -> float:
        try:
            value = float(self._opt(path, default))
        except (TypeError, ValueError):
            return default
        if value != value:  # NaN
            return default
        return min(max(value, 0.0), 1.0)

    # ---------------------------------------------------------------- 状态

    def _evict_sessions(self) -> None:
        """控制会话状态表的规模，避免长期运行后无限增长。"""
        if len(self._sessions) < MAX_TRACKED_SESSIONS:
            return
        now = time.time()
        for key in [
            k for k, v in self._sessions.items() if now - v.touched > SESSION_TTL_SECONDS
        ]:
            self._sessions.pop(key, None)
        if len(self._sessions) >= MAX_TRACKED_SESSIONS:
            # 还超就按最久未活动淘汰四分之一
            stale = sorted(self._sessions.items(), key=lambda kv: kv[1].touched)
            for key, _ in stale[: max(1, len(stale) // 4)]:
                self._sessions.pop(key, None)

    def _state(self, event: AstrMessageEvent) -> SessionState:
        umo = event.unified_msg_origin
        state = self._sessions.get(umo)
        if state is None:
            self._evict_sessions()
            state = SessionState()
            self._sessions[umo] = state
        state.touched = time.time()
        return state

    def _roll(self, probability: float) -> bool:
        return self._rng.random() < probability

    # -------------------------------------------------------------- 目标选择

    @staticmethod
    def _mentioned_ids(event: AstrMessageEvent) -> list[tuple[str, str]]:
        """返回本条消息里 @ 到的人：[(qq, 昵称), ...]，跳过 @全体成员。"""
        result: list[tuple[str, str]] = []
        try:
            messages = event.get_messages() or []
        except Exception:
            return result
        for comp in messages:
            if not isinstance(comp, At):
                continue
            qq = str(getattr(comp, "qq", "") or "").strip()
            if not qq or qq == "all":
                continue
            name = str(getattr(comp, "name", "") or "").strip()
            result.append((qq, name))
        return result

    def _pick_target(self, event: AstrMessageEvent) -> str:
        """按 ``target.mode`` 选出默认要戳的人；选不出返回空串。"""
        mode = str(self._opt("target.mode", "sender") or "sender").strip().lower()
        if mode == "mentioned_first":
            mentioned = self._mentioned_ids(event)
            if mentioned:
                return mentioned[0][0]
        elif mode == "random_member":
            target = self._random_group_member(event)
            if target:
                return target
        return str(event.get_sender_id() or "").strip()

    def _random_group_member(self, event: AstrMessageEvent) -> str:
        """从当前会话已缓存的群成员里随机挑一个（拿不到就返回空串）。

        这里刻意**不发**群成员列表接口：那是个重接口，为了随机戳一下不值得。
        只从 ``event.message_obj.group.members`` 里挑——适配器在别处拉过成员列表时才有值。
        """
        group = getattr(getattr(event, "message_obj", None), "group", None)
        members = getattr(group, "members", None) or []
        self_id = str(event.get_self_id() or "")
        candidates = [
            str(getattr(m, "user_id", "") or "")
            for m in members
            if str(getattr(m, "user_id", "") or "") and str(getattr(m, "user_id", "")) != self_id
        ]
        return self._rng.choice(candidates) if candidates else ""

    def _resolve_target(self, event: AstrMessageEvent, spec: str) -> str:
        """把 LLM 给的 target 说明解析成 QQ 号。

        支持：纯数字（QQ 号）、消息里 @ 到的昵称、空（发消息的人）。
        """
        spec = (spec or "").strip()
        if not spec:
            return str(event.get_sender_id() or "").strip()
        if spec.isdigit():
            return spec
        mentioned = self._mentioned_ids(event)
        for qq, name in mentioned:
            if name and (name == spec or spec in name or name in spec):
                return qq
        for qq, _ in mentioned:
            if qq == spec:
                return qq
        return ""

    # ---------------------------------------------------------------- 发送

    @staticmethod
    def _bot(event: AstrMessageEvent) -> Any:
        return getattr(event, "bot", None)

    @staticmethod
    def _routing(event: AstrMessageEvent) -> dict[str, Any]:
        """多账号场景下 aiocqhttp 需要 self_id 路由（AstrBot 自己也是这么传的）。"""
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        getter = getattr(raw, "get", None)
        self_id = getter("self_id") if callable(getter) else getattr(raw, "self_id", None)
        return {"self_id": self_id} if self_id else {}

    async def _call(self, event: AstrMessageEvent, action: str, **params: Any) -> Any:
        bot = self._bot(event)
        if bot is None:
            raise RuntimeError("当前平台适配器不支持 OneBot 接口（需要 aiocqhttp）")
        payload = {**self._routing(event), **params}
        for obj in (getattr(bot, "api", None), bot):
            fn = getattr(obj, "call_action", None)
            if callable(fn):
                return await fn(action, **payload)
        direct = getattr(bot, action, None)
        if callable(direct):
            return await direct(**payload)
        raise RuntimeError(f"没有可用的调用方式: {action}")

    async def _poke_via_api(self, event: AstrMessageEvent, target: str) -> None:
        """用 OneBot 接口戳一戳。

        参数语义（NapCat 源码 ``action/packet/SendPoke.ts``）：

        - ``user_id`` **必填**，缺了会 schema 校验失败（400/1400）；
        - 带 ``group_id`` = 群戳，不带 = 私聊戳；
        - 群号传空字符串会被判成私聊戳，所以这里必须按真假值分支；
        - 成功时 ``data`` 是 undefined，不要依赖返回值，只要没抛异常就算成功。

        ``group_poke`` / ``friend_poke`` / ``send_poke`` 在 NapCat 里其实是**同一个 handler**，
        但其它协议端（如 go-cqhttp）只实现了 ``send_poke``，所以这里按顺序都试一遍。
        """
        group_id = str(event.get_group_id() or "").strip()
        if group_id:
            attempts = [
                ("group_poke", {"group_id": _as_int(group_id), "user_id": _as_int(target)}),
                ("send_poke", {"group_id": _as_int(group_id), "user_id": _as_int(target)}),
            ]
        else:
            attempts = [
                ("friend_poke", {"user_id": _as_int(target)}),
                ("send_poke", {"user_id": _as_int(target)}),
            ]

        last_error: Exception | None = None
        for action, params in attempts:
            try:
                await self._call(event, action, **params)
                self._note_send_method(f"api:{action}")
                return
            except Exception as exc:  # noqa: BLE001 - 逐个协议端探测
                last_error = exc
        raise last_error if last_error else RuntimeError("戳一戳接口全部失败")

    async def _poke_via_segment(self, event: AstrMessageEvent, target: str) -> None:
        """把 poke 消息段塞进普通消息发送。

        ⚠️ **NapCat 不支持这种方式**：它源码里 poke 的发送转换器就是 ``async () => undefined``
        （``api/msg.ts`` 的 ``ob11ToRawConverters``），poke 段会被静默丢弃；
        如果消息里只有 poke 段，还会直接报「消息体无法解析」。
        保留这个分支只是为了个别支持 poke 段的协议端，默认不会走到。
        """
        segment = _poke_segment(target)
        group_id = str(event.get_group_id() or "").strip()
        if group_id:
            await self._call(
                event,
                "send_group_msg",
                group_id=_as_int(group_id),
                message=[segment],
            )
        else:
            await self._call(
                event,
                "send_private_msg",
                user_id=_as_int(event.get_sender_id()),
                message=[segment],
            )
        self._note_send_method("segment")

    def _note_send_method(self, method: str) -> None:
        """第一次成功时记一条日志，方便用户知道协议端最终走的是哪条路。"""
        if self._logged_send_method != method:
            self._logged_send_method = method
            logger.info(f"戳一戳发送方式确定: {method}")

    # ------------------------------------------------------------ 送达确认

    def _arm_confirm(self, target: str) -> asyncio.Future[bool]:
        """登记一个「等这个目标被戳」的确认点。"""
        old = self._pending_confirms.get(target)
        if old is not None and not old.done():
            old.cancel()
        fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._pending_confirms[target] = fut
        return fut

    def _resolve_confirm(self, target: str) -> bool:
        """协议端回传了「戳了 target」，点亮确认点。"""
        fut = self._pending_confirms.get(target)
        if fut is None or fut.done():
            return False
        fut.set_result(True)
        return True

    async def _await_confirm(self, target: str, fut: asyncio.Future[bool]) -> bool:
        """等确认事件；等到返回 True，超时返回 False。"""
        try:
            await asyncio.wait_for(fut, CONFIRM_TIMEOUT_SECONDS)
            logger.info(f"戳一戳送达已确认: {target}")
            return True
        except asyncio.TimeoutError:
            logger.warning(
                f"戳一戳接口调用成功，但 {CONFIRM_TIMEOUT_SECONDS:.0f} 秒内没收到送达事件: "
                f"{target}。可能并没有真的发出去（对方屏蔽戳一戳 / 非好友 / "
                f"协议端 packetBackend 异常），请自行确认。"
            )
            return False
        finally:
            if self._pending_confirms.get(target) is fut:
                self._pending_confirms.pop(target, None)

    @staticmethod
    def _parse_poke_notice(event: AstrMessageEvent) -> tuple[str, str] | None:
        """从 poke 通知里取出 ``(戳的人, 被戳的人)``；不是 poke 通知就返回 None。

        字段含义（NapCat 的 ``OB11GroupPokeEvent`` / ``OB11FriendPokeEvent``）：

        - 群聊：``user_id`` = 戳的人，``target_id`` = 被戳的人
        - 私聊：``sender_id`` = 戳的人，``user_id`` = 会话对端（可能正是被戳的人），
          ``target_id`` = 被戳的人
        """
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        get = getattr(raw, "get", None)
        if not callable(get):
            return None
        try:
            if get("post_type") != "notice" or get("notice_type") != "notify":
                return None
            if get("sub_type") != "poke":
                return None
            pokee = str(get("target_id") or "").strip()
            poker = str(get("sender_id") or get("user_id") or "").strip()
        except Exception:
            return None
        if not poker or not pokee or pokee == "0":
            return None
        return poker, pokee

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def watch_poke_delivery(self, event: AstrMessageEvent) -> None:
        """观察 poke 通知，确认机器人自己发出的戳一戳有没有真的送出去。

        这个 handler 只读不写：不停止事件、不发消息，纯粹用来点亮送达确认。
        """
        info = self._parse_poke_notice(event)
        if info is None:
            return
        poker, pokee = info
        if poker != str(event.get_self_id() or ""):
            return  # 不是机器人自己发的戳
        self._resolve_confirm(pokee)

    async def _deliver(self, event: AstrMessageEvent, target: str) -> PokeOutcome:
        """真正把戳一戳发出去。

        默认走 OneBot 接口。**不再自动降级到 poke 消息段**——
        NapCat 压根不支持用消息段发戳一戳（会被静默丢弃或直接报错），
        降级只会白白多打一次接口调用。真想试消息段就显式配 ``send_method=segment``。
        """
        method = str(self._opt("advanced.send_method", "auto") or "auto").lower()
        sender = self._poke_via_segment if method == "segment" else self._poke_via_api
        try:
            await sender(event, target)
        except Exception as exc:  # noqa: BLE001 - 失败原因要回给用户/LLM
            return PokeOutcome(False, _explain_poke_error(exc))
        return PokeOutcome(True, f"已戳 {target}")

    # ------------------------------------------------------------ 统一的出口

    def _group_allowed(self, event: AstrMessageEvent) -> bool:
        group_id = str(event.get_group_id() or "").strip()
        if not group_id:
            return True  # 私聊不参与群过滤
        white = _as_id_list(self._opt("limits.group_whitelist", []))
        black = _as_id_list(self._opt("limits.group_blacklist", []))
        if white and group_id not in white:
            return False
        if black and group_id in black:
            return False
        return True

    async def _is_admin(self, event: AstrMessageEvent, target: str) -> bool:
        group_id = str(event.get_group_id() or "").strip()
        if not group_id:
            return False
        try:
            info = await self._call(
                event,
                "get_group_member_info",
                group_id=_as_int(group_id),
                user_id=_as_int(target),
                no_cache=False,
            )
        except Exception:
            return False
        role = str((info or {}).get("role", "") or "") if isinstance(info, dict) else ""
        return role in ("owner", "admin")

    async def _try_poke(
        self,
        event: AstrMessageEvent,
        target: str,
        scenario: str,
        *,
        wait_confirm: bool = False,
    ) -> PokeOutcome:
        """所有场景的唯一出口：做完全部检查再发送。

        Args:
            scenario: ``before_reply`` / ``on_silent`` / ``by_llm``，决定用哪一组冷却配置。
            wait_confirm: 是否阻塞等待协议端的送达确认。
                LLM 工具要等（它得如实回答「戳成功了没」），
                自动场景不等（不能为了确认拖慢正在发送的回复），改成后台确认并记日志。
        """
        target = str(target or "").strip()
        self_id = str(event.get_self_id() or "").strip()

        if not target or not target.isdigit():
            return PokeOutcome(False, "没有可戳的目标")
        if self_id and target == self_id:
            return PokeOutcome(False, "不能戳机器人自己")
        if not self._group_allowed(event):
            return PokeOutcome(False, "当前群不在允许范围内")

        now = time.time()
        state = self._state(event)

        # 场景自带的冷却 / 一轮对话窗口
        if scenario == "before_reply":
            window = self._opt_int(
                "poke_before_reply.round_window", 300, low=0, high=86400
            )
            if window > 0 and now - state.last_before_reply < window:
                return PokeOutcome(False, "这一轮对话已经戳过了")
        elif scenario == "on_silent":
            group_cd = self._opt_int(
                "poke_on_silent.group_cooldown", 60, low=0, high=86400
            )
            if group_cd > 0 and now - state.last_on_silent < group_cd:
                return PokeOutcome(False, "本群刚戳过")
            user_cd = self._opt_int(
                "poke_on_silent.user_cooldown", 600, low=0, high=86400
            )
            if user_cd > 0 and now - state.user_last.get(target, 0.0) < user_cd:
                return PokeOutcome(False, "这个人最近戳过了")
        elif scenario == "by_llm":
            cd = self._opt_int("poke_by_llm.cooldown", 120, low=0, high=86400)
            if cd > 0 and now - state.last_by_llm < cd:
                return PokeOutcome(False, "本会话刚戳过，稍后再来")

        # 全局保护：同一会话的最小间隔 + 每小时上限
        min_interval = self._opt_int("limits.min_interval", 30, low=0, high=86400)
        if min_interval > 0 and state.pokes and now - state.pokes[-1] < min_interval:
            return PokeOutcome(False, "两次戳之间太近了")
        max_per_hour = self._opt_int("limits.max_per_hour", 20, low=0, high=10000)
        if max_per_hour > 0:
            recent = [t for t in state.pokes if now - t < RATE_WINDOW_SECONDS]
            if len(recent) >= max_per_hour:
                return PokeOutcome(False, "本会话本小时已达上限")

        # 不戳群主 / 管理员（需要一次接口调用，放在最后以免浪费）
        if self._opt_bool("target.ignore_admins", False) and await self._is_admin(
            event, target
        ):
            return PokeOutcome(False, "对方是群主或管理员，按配置跳过")

        if self._opt_bool("advanced.dry_run", False):
            state.pokes.append(now)
            self._stamp(state, target, scenario, now)
            logger.info(
                f"[dry-run] 本该戳 {target}（场景={scenario}, 群={event.get_group_id() or '私聊'}）"
            )
            # ⚠️ sent=False：dry-run 只是「演练」，绝不能对外报告成戳成功了
            return PokeOutcome(
                True, "[dry-run] 只记日志，没有真的发送", sent=False
            )

        try:
            outcome = await self._deliver(event, target)
        except Exception as exc:  # 兜底，绝不让插件异常冒到框架
            outcome = PokeOutcome(False, str(exc)[:200])

        if not outcome.ok:
            logger.warning(f"戳 {target} 失败（场景={scenario}）: {outcome.detail}")
            return outcome

        state.pokes.append(now)
        self._stamp(state, target, scenario, now)
        logger.info(
            f"戳了 {target}（场景={scenario}, 群={event.get_group_id() or '私聊'}）"
        )

        # 接口成功只是「包发出去了」。用协议端回传的 poke 事件确认是否真的送出去。
        fut = self._arm_confirm(target)
        if wait_confirm:
            confirmed = await self._await_confirm(target, fut)
            return PokeOutcome(True, outcome.detail, sent=True, confirmed=confirmed)

        task = asyncio.create_task(self._await_confirm(target, fut))
        self._confirm_tasks.add(task)
        task.add_done_callback(self._confirm_tasks.discard)
        return outcome

    @staticmethod
    def _stamp(
        state: SessionState, target: str, scenario: str, now: float
    ) -> None:
        state.user_last[target] = now
        if len(state.user_last) > 200:
            # 只保留最近的 100 条，防止无限增长
            for key, _ in sorted(state.user_last.items(), key=lambda kv: kv[1])[:100]:
                state.user_last.pop(key, None)
        if scenario == "before_reply":
            state.last_before_reply = now
        elif scenario == "on_silent":
            state.last_on_silent = now
        elif scenario == "by_llm":
            state.last_by_llm = now

    # -------------------------------------------------- 场景一：回复前戳一戳

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.on_decorating_result()
    async def poke_before_reply(self, event: AstrMessageEvent) -> None:
        """机器人准备回复之前，按概率先戳对方一下。

        ``on_decorating_result`` 由 ``ResultDecorateStage`` 触发，位于 ``RespondStage`` 之前，
        所以这里确实是在「回复发出去之前」。
        """
        if not self._opt_bool("poke_before_reply.enable", True):
            return
        # 别人戳机器人时，AstrBot 会把这条 notice 当成一条只含 Poke 组件的消息。
        # 私聊里这种消息会唤醒机器人并让 LLM 回复，进而触发本钩子 —— 不拦住就会互戳。
        if self._is_poke_notice(event):
            return
        result = event.get_result()
        if result is None or not getattr(result, "chain", None):
            return  # 没有真正要发出去的内容，不算一次对话

        probability = self._opt_probability("poke_before_reply.probability", 0.3)
        if probability <= 0 or not self._roll(probability):
            return

        target = self._pick_target(event)
        await self._try_poke(event, target, "before_reply")

    # ------------------------------------------------ 场景二：不回复时戳一戳

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def poke_on_silent(self, event: AstrMessageEvent) -> None:
        """群里有人发言但没触发回复时，用更低的概率戳他一下。

        关键：注册这条「所有群消息」的 handler 会让 ``WakingCheckStage`` 把事件标记为
        ``is_wake = True``，但不会设置 ``is_at_or_wake_command``。
        ``ProcessStage`` 的默认 LLM 请求要求后者为真，所以观察非唤醒消息不会让机器人到处回复。
        """
        if not self._opt_bool("poke_on_silent.enable", True):
            return
        # 消息本身就唤醒了机器人（@ / 引用机器人 / 唤醒前缀）→ 交给场景一
        if getattr(event, "is_at_or_wake_command", False):
            return
        # 别人戳机器人也会作为一条只含 Poke 的消息进来，必须跳过，否则会互戳
        if self._is_poke_notice(event):
            return
        # 群成员进出之类的通知事件也会被映射成 GROUP_MESSAGE（消息链为空），跳过
        try:
            if not (event.get_messages() or []):
                return
        except Exception:
            return

        probability = self._opt_probability("poke_on_silent.probability", 0.05)
        if probability <= 0 or not self._roll(probability):
            return

        target = self._pick_target(event)
        await self._try_poke(event, target, "on_silent")

    @staticmethod
    def _is_poke_notice(event: AstrMessageEvent) -> bool:
        """这条消息是不是「别人戳了机器人」的通知（消息链里只有 Poke 组件）。"""
        try:
            comps = event.get_messages() or []
        except Exception:
            return False
        if not comps:
            return False
        return all(isinstance(c, Poke) for c in comps)

    # ---------------------------------------------- 场景三：交给机器人自己决定

    @filter.on_llm_request()
    async def inject_poke_context(
        self, event: AstrMessageEvent, req: Any
    ) -> None:
        """告诉 LLM 它现在可以戳谁。

        LLM 看不到 QQ 号，所以这里把「发消息的人」和「本条消息 @ 到的人」列给它，
        ``poke`` 工具才能用昵称或号码指名道姓。
        """
        if not self._opt_bool("poke_by_llm.enable", True):
            return
        if not self._opt_bool("poke_by_llm.allow_mentioned", True):
            return
        mentioned = self._mentioned_ids(event)
        if not mentioned:
            return

        roster = "、".join(f"{name or '某人'}({qq})" for qq, name in mentioned)
        hint = (
            "\n\n[戳一戳] 本条消息 @ 了：" + roster + "。"
            "如果你觉得戳一下比回消息更合适，可以调用 poke 工具，target 填上面的 QQ 号或昵称。"
            "同一会话不要频繁使用。"
        )
        try:
            current = getattr(req, "system_prompt", "") or ""
            req.system_prompt = f"{current}{hint}"
        except Exception as exc:  # pragma: no cover - 不同版本字段可能不同
            logger.debug(f"注入戳一戳提示失败: {exc}")

    @filter.llm_tool(name="poke")
    async def poke_tool(
        self, event: AstrMessageEvent, target: str = "", reason: str = ""
    ) -> str:
        """戳一戳某个人（相当于双击对方头像）。

        当你觉得「戳一下」比发消息更合适时使用，比如想引起对方注意、表达情绪、
        或者对话里 @ 到了某个人想跟他互动。不要频繁使用，同一个会话短时间内只能戳一次。

        Args:
            target(string): 要戳的人的 QQ 号；也可以填消息里 @ 到的昵称；留空表示戳当前和你说话的人。
            reason(string): 一句话说明你为什么戳他，会记录到日志里。
        """
        if not self._opt_bool("poke_by_llm.enable", True):
            return "戳一戳功能已关闭。"

        resolved = self._resolve_target(event, target)
        if not resolved:
            return (
                "没有找到要戳的人。target 需要填 QQ 号，"
                "或者本条消息 @ 到的人的昵称。"
            )

        outcome = await self._try_poke(event, resolved, "by_llm", wait_confirm=True)
        note = f"（原因：{reason.strip()}）" if reason and reason.strip() else ""

        if not outcome.ok:
            return f"这次没戳成：{outcome.detail}"
        if not outcome.sent:
            # ⚠️ dry-run 只是演练，必须如实告诉 LLM，否则它会跟用户说「已经戳了」
            return (
                f"当前是 dry-run 演练模式，**没有真的戳** {resolved}（只写了日志）。"
                f"请告诉用户：插件当前只演练不发送，需要管理员关掉「只记日志不真的戳」才会真的戳。"
            )
        if outcome.confirmed is False:
            return (
                f"接口调用成功了，但 {CONFIRM_TIMEOUT_SECONDS:.0f} 秒内没收到戳一戳送达事件，"
                f"所以**不能确定真的戳到** {resolved}（对方可能屏蔽了戳一戳、不是好友，"
                f"或者协议端根本没发出去）。请如实告诉用户「调用了但不确定成功」。"
            )
        return f"已经戳了 {resolved}，并收到了送达确认。{note}"

    # ---------------------------------------------------------------- 收尾

    async def terminate(self):
        """插件卸载时清掉状态与后台任务。"""
        for task in list(self._confirm_tasks):
            task.cancel()
        if self._confirm_tasks:
            await asyncio.gather(*self._confirm_tasks, return_exceptions=True)
        self._confirm_tasks.clear()
        for fut in list(self._pending_confirms.values()):
            if not fut.done():
                fut.cancel()
        self._pending_confirms.clear()
        self._sessions.clear()
