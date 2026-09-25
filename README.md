# astrbot_plugin_proactive_poke

**让机器人主动戳一戳别人。** 三种触发场景，各自独立开关与概率。

`版本 1.0.0` · `平台 aiocqhttp (OneBot v11)` · `许可 MIT` · `第三方依赖 无`

适配 **NapCat** / LLOneBot / Lagrange.Core 等 QQ 协议端，即 AstrBot 的 aiocqhttp 适配器。

---

## 目录

- [三句话说明](#三句话说明)
- [安装](#安装)
- [三个场景](#三个场景)
  - [场景一 回复前戳](#场景一-回复前戳)
  - [场景二 不回复时戳](#场景二-不回复时戳)
  - [场景三 机器人自己决定](#场景三-机器人自己决定)
- [配置](#配置)
  - [通用设置](#通用设置)
  - [戳谁](#戳谁)
  - [频率与范围](#频率与范围)
  - [高级](#高级)
- [工作原理](#工作原理)
- [已知限制](#已知限制)
- [常见问题](#常见问题)
  - [为什么不能用 poke 消息段发送](#为什么不能用-poke-消息段发送)
  - [机器人说戳了但对方没反应](#机器人说戳了但对方没反应)
  - [报 packetBackend 发包能力不可用](#报-packetbackend-发包能力不可用)
  - [会不会和别人互戳不停](#会不会和别人互戳不停)
- [开发与测试](#开发与测试)
- [相关链接](#相关链接)
- [许可](#许可)

---

## 三句话说明

1. **回复前戳**：机器人准备回复某人之前，按概率先戳他一下；同一会话一轮对话只戳一次。
2. **不回复时戳**：群里有人发言但没触发回复时，用更低的概率戳他。
3. **机器人自己决定**：给 LLM 一个 `poke` 工具，它觉得「戳一下比发消息合适」时自己戳，
   也可以在消息 @ 到某人时戳那个人。

## 安装

不依赖任何第三方库，无需 `requirements.txt`。

- **插件市场**：WebUI → 插件 → 搜索「主动戳一戳」→ 安装
- **从 GitHub**：WebUI → 插件 → 安装插件，填
  `https://github.com/suwa-ko/astrbot_plugin_proactive_poke`
- **手动**：把 `astrbot_plugin_proactive_poke` 目录放进 `<AstrBot 根目录>/data/plugins/`，
  然后在 WebUI 插件页重载

## 三个场景

三个场景互相独立，可以只开一个、开两个、或全开。**同一次戳只会计入一个场景。**

### 场景一 回复前戳

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `poke_before_reply.enable` | `true` | 开关 |
| `poke_before_reply.probability` | `0.3` | 触发概率 |
| `poke_before_reply.round_window` | `300` | 一轮对话的间隔（秒） |

挂在 AstrBot 的 `on_decorating_result` 钩子上——它由 `ResultDecorateStage` 触发，
在 `RespondStage` **之前**，所以是真正的「回复发出去之前」。

「一轮完整对话只戳一次」是用**时间窗**界定的：戳过一次后，这个会话在
`round_window` 秒内不会再戳。你和机器人来回聊十句，只会被戳一次；
隔了五分钟再来，才算新的一轮。

### 场景二 不回复时戳

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `poke_on_silent.enable` | `true` | 开关 |
| `poke_on_silent.probability` | `0.05` | 触发概率（建议明显低于场景一） |
| `poke_on_silent.user_cooldown` | `600` | 对同一个人的冷却（秒） |
| `poke_on_silent.group_cooldown` | `60` | 同一个群的冷却（秒） |

**只在群里生效**：私聊里任何消息都会唤醒机器人，不存在「不回复」的情况。

判定方式见[工作原理](#工作原理)——简单说就是「这条消息本来不会唤醒机器人」。

### 场景三 机器人自己决定

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `poke_by_llm.enable` | `true` | 开关 |
| `poke_by_llm.cooldown` | `120` | 同一会话的冷却（秒） |
| `poke_by_llm.allow_mentioned` | `true` | 允许戳消息里 @ 到的人 |

插件会注册一个名叫 `poke` 的 LLM 工具，参数是 `target`（QQ 号 / @ 到的昵称 / 留空=发消息的人）
和 `reason`（为什么要戳，会记进日志）。

LLM 看不到 QQ 号，所以开启 `allow_mentioned` 时，插件会在请求里注入一行提示，
把「本条消息 @ 了谁」告诉它，它才能指名道姓地戳。

## 配置

### 通用设置

三个场景的配置在各自的组里，下面这些是共用的。

### 戳谁

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `target.mode` | `sender` | `sender` 发消息的人 / `mentioned_first` 第一个被 @ 的人（没有就退回 sender）/ `random_member` 群里随机一人 |
| `target.ignore_admins` | `false` | 不戳群主和管理员 |

> `random_member` 只从**已经缓存过的**群成员里挑（`event.message_obj.group.members`）。
> 刻意不去调 `get_group_member_list`——那是个重接口，为了随机戳一下不值得。
> 拿不到缓存时就退回 `sender`。

### 频率与范围

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `limits.max_per_hour` | `20` | 每会话每小时最多戳几次（三个场景共用，硬上限） |
| `limits.min_interval` | `30` | 同一会话两次戳的最小间隔（秒） |
| `limits.group_whitelist` | `[]` | 群白名单，留空表示所有群 |
| `limits.group_blacklist` | `[]` | 群黑名单 |

> QQ 对戳一戳**没有公开的频率阈值**，NapCat 侧也**完全没有限速**
> （`send_poke_rate_limited` 在 NapCat 里就是 `send_poke`，不做任何排队）。
> 所以插件自己做了这三层限制。社区现成实现的经验值是「0.5 秒间隔、单人 10 秒冷却」，
> 本插件的默认值更保守。

### 高级

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `advanced.send_method` | `auto` | `auto` 走 OneBot 接口（唯一在 NapCat 上有效的方式）；`segment` 用 poke 消息段，**NapCat 不支持** |
| `advanced.dry_run` | `false` | 只演练不真的戳，调参时用。**不是静默模式**：机器人知道当前没真戳，你问它它会如实说 |

开了 `dry_run` 后日志里会显示「本该戳谁、由哪个场景触发」，但**仍然会计入冷却和上限**，
这样调出来的参数才有意义。

> `dry_run` 下机器人**不会**骗你说戳成功了——LLM 工具会返回
> 「dry-run 演练模式，没有真的戳」。这是刻意设计的：早期版本会把 dry-run 当成成功，
> 你去问它戳没戳，它会说「已经戳了」，但实际什么都没发。

## 工作原理

### 场景二怎么知道「这条消息不会触发回复」

AstrBot 的 `WakingCheckStage` 有个容易忽略的行为：**插件的 handler 只要 filter 通过，
就会把事件标记为 `is_wake = True`**（这是它文档里列的第 4 条唤醒条件）。

所以注册一条「所有群消息」的 handler 会让机器人对**每条消息**都醒来。
但它**不会**设置 `is_at_or_wake_command`——那个标志只由「@ 机器人 / 引用机器人 /
唤醒前缀 / 私聊」这几种消息级条件设置。

而 `ProcessStage` 里默认的 LLM 请求要求 `is_at_or_wake_command` 为真：

```python
if (not event._has_send_oper and event.is_at_or_wake_command and not event.call_llm):
```

**实测结果**（真实 AstrBot 4.28.1 + 真实 `WakingCheckStage` + `ProcessStage`）：

| 场景 | 我们的 handler | `is_at_or_wake_command` | LLM 调用 |
| --- | --- | --- | --- |
| 群里普通聊天（无 @ 无引用） | ✅ 跑了 1 次 | **False** | **0 次** |
| 群里 @ 机器人 | ✅ 跑了 1 次 | True | 1 次 |

所以插件能观察到每条群消息，又**不会**让机器人到处回复。
这个行为有专门的回归测试盯着（`tests/poke/test_integration.py`）。

### 发送

只走 OneBot 接口，按协议端能力依次尝试：

| 场景 | 先试 | 再试 |
| --- | --- | --- |
| 群聊 | `group_poke(group_id, user_id)` | `send_poke(group_id, user_id)` |
| 私聊 | `friend_poke(user_id)` | `send_poke(user_id)` |

`user_id` 是**必填**的（NapCat 的 schema 要求）；群号传空字符串会被当成私聊戳，
所以代码里按真假值分支。成功时接口返回的 `data` 是 undefined，插件不依赖返回值，
只要没抛异常就算成功。

> 在 NapCat 里 `group_poke` / `friend_poke` / `send_poke` **其实是同一个 handler**，
> 但其它协议端（如 go-cqhttp）只实现了 `send_poke`，所以还是按顺序都试一遍。

### 送达确认：接口成功 ≠ 对方收到

这是戳一戳最容易踩的坑。NapCat 的 poke 接口**只要包发出去了就返回成功**，
它不做任何校验——不查好友关系、不查群成员、不查对方有没有屏蔽戳一戳。
所以「接口返回 ok」和「对方真的被戳了」完全是两回事。

唯一可靠的反馈是协议端随后上报的 poke 事件（机器人自己发的戳也会上报）。
插件就用它做确认：

1. 发送成功后登记一个确认点
2. `watch_poke_delivery` handler 盯着 poke 通知，看到「机器人戳了 X」就点亮它
3. **LLM 工具会等这个确认**（最多 3 秒），然后如实回答：

| 情况 | 工具返回给 LLM |
| --- | --- |
| 收到送达事件 | 「已经戳了 X，并收到了送达确认」 |
| 没收到 | 「接口调用成功了，但没收到送达事件，**不能确定真的戳到** X」 |
| `dry_run` | 「演练模式，**没有真的戳** X」 |

自动场景（一、二）**不等确认**——那会拖慢正在发送的回复。它们改成后台确认，
结果只写日志：确认到了记 INFO，没确认到记 WARNING 并说明可能的原因。

> 排查「机器人说戳了但对方没反应」时，直接搜日志里的
> `戳一戳送达已确认` / `没收到送达事件`，一眼就能看出是接口没发出去还是对方那边没收到。

### 不会互戳

别人戳机器人时，AstrBot 会把这条 notice 当成一条**只含 `Poke` 组件的消息**投进来。
在私聊里，这种消息会唤醒机器人 → 触发 LLM 回复 → 触发场景一 → **戳回去** → 无限互戳。

所以两个自动场景都会先判断「消息链里是不是只有 Poke 组件」，是就跳过。
场景三（LLM 决定）不受这条限制，因为那本来就是它的自主判断，且有 120 秒冷却兜底。

## 已知限制

| 限制 | 说明 |
| --- | --- |
| 只能在 aiocqhttp 上用 | 插件的 handler 都带了 `platform_adapter_type(AIOCQHTTP)` |
| 私聊不能用来「不回复时戳」 | 私聊任何消息都会唤醒机器人，场景二会自动跳过 |
| 戳一戳依赖 NapCat 的 packetBackend | 这是 NapCat 通过原生 hook 注入 OIDB（0xED3）实现的，受 QQ 版本白名单限制；不在白名单里就只能失败 |
| 无法确认「真的戳到了」 | QQ 服务端对戳一戳没有任何前置校验，接口成功 ≠ 对方收到（对方可能屏蔽了戳一戳，或非好友）。唯一可靠的反馈是后续收到的 poke 事件 |
| 私聊戳需要好友关系 | 对方不是好友时服务端会拒绝 |
| 没有批量接口 | 想戳多个人只能一个个来，协议层没有「戳全体成员」 |
| 频率阈值无权威数据 | QQ 侧没有公开数字，插件的默认值只是保守估计 |

## 常见问题

### 机器人说戳了但对方没反应

按这个顺序查：

1. **先看是不是 dry-run**。配置里 `advanced.dry_run` 打开时插件只演练不发送。
   现在的版本会如实告诉 LLM「没有真的戳」，所以机器人不该说"戳了"；
   如果它说了，说明你装的是旧版本，先升级再关掉 dry_run。
2. **搜日志 `戳一戳送达已确认`**。有这行说明协议端确实把戳发出去了，
   问题在对方那边（屏蔽了戳一戳 / 非好友 / QQ 客户端没提示）。
3. **搜日志 `没收到送达事件`**。说明接口调用成功了，但协议端没回传发送事件——
   通常意味着包根本没发出去。接着看有没有 `packetBackend` 相关的报错。
4. **搜日志 `戳一戳发送方式确定`**。它会告诉你协议端最终走的是哪个 action
   （`api:group_poke` / `api:friend_poke` / `api:send_poke`），确认路径对不对。
5. **私聊戳需要好友关系**，群内戳需要双方都在群里。这两个 NapCat 都不校验，
   但 QQ 服务端会拒绝。

### 为什么不能用 poke 消息段发送

NapCat 源码里 poke 段的发送转换器就是：

```ts
[OB11MessageDataType.poke]: async () => undefined,
```

`createSendElements` 会把这些 `undefined` 过滤掉，所以：
- 在 `send_group_msg` 里夹一个 poke 段 → **被静默丢弃**（你不会收到任何错误）
- 消息里**只有** poke 段 → 直接报 `消息体无法解析, 请检查是否发送了不支持的消息类型`

CQ 码 `[CQ:poke,id=1]` 走的是同一套转换器，同样无效。
官方文档也标注了 poke 是「事件上报与接口调用**不通过消息**」。

所以本插件默认只用接口发送，**不做消息段降级**——那只会白打一次接口调用。
`send_method=segment` 保留给个别支持 poke 段的协议端。

### 报 packetBackend 发包能力不可用

说明 NapCat 的原生发包能力没启用，通常是因为 **QQ 版本不在 NapCat 的支持白名单内**。
处理办法：

1. 看 NapCat 的启动日志，确认 packetBackend 的状态
2. 参考 NapCat 官方「高级配置」文档 <https://doc.napneko.icu/config/advanced>
3. 必要时换一个白名单内的 QQ 版本

插件会把这句很长的报错翻译成一句人话再显示。

### 会不会和别人互戳不停

不会。自动场景（一、二）都会跳过「只含 Poke 组件」的事件，
而机器人自己发的戳也会作为 poke 通知上报回来，所以不会自激。

另外三层频率限制（每会话每小时上限、最小间隔、场景自带冷却）也兜住了。

## 开发与测试

```powershell
pwsh -File tests/run_all_tests.ps1          # 工作区里所有插件的测试
pwsh -File tests/run_all_tests.ps1 poke     # 只跑本插件
```

| 层次 | 文件 | 依赖 | 验证内容 |
| --- | --- | --- | --- |
| 离线逻辑测试 | `tests/poke/run_tests.py` | 无（用 `tests/stubs` 里的桩模块） | 三个场景的概率门槛与冷却、一轮对话只戳一次、poke 通知与空消息链的跳过、目标解析（QQ 号 / 昵称 / 留空）、不戳自己、白黑名单、每小时上限、最小间隔、管理员跳过、dry-run 不谎报成功、发送降级与 packetBackend 报错翻译、**送达确认（poke 通知解析与点亮、未确认时如实说「不确定」）** |
| 真实框架集成测试 | `tests/poke/test_integration.py` | 真实 `astrbot` + 真实 `aiocqhttp`（Python 3.12） | 4 个 handler 注册到正确的事件类型、`poke` 工具的 docstring 参数解析、**真实 `WakingCheckStage` 下非唤醒消息会激活 handler 但不触发 LLM**、@ 机器人仍然正常回复、真实 `call_event_hook(OnDecoratingResultEvent)` 能触发回复前戳 |

集成测试环境（本机验证通过，AstrBot 4.28.1 / Python 3.12.14）：

```powershell
pip install uv
python -m uv python install 3.12
python -m uv venv --python 3.12 "$env:TEMP\astrbot-env"
python -m uv pip install --python "$env:TEMP\astrbot-env\Scripts\python.exe" astrbot==4.28.1
```

> ⚠️ **两层测试都没有覆盖的**：真实 QQ + 真实 NapCat。戳一戳的实际送达、
> packetBackend 可用性、风控阈值都只能在真机上验证。

## 相关链接

- 插件仓库：<https://github.com/suwa-ko/astrbot_plugin_proactive_poke>
- [AstrBot 插件开发文档](https://docs.astrbot.app/dev/star/plugin-new.html)
- [NapCat 高级配置（packetBackend）](https://doc.napneko.icu/config/advanced)
- [NapCat API 接口列表](https://doc.napneko.icu/onebot/api)

---

## 许可

[MIT](LICENSE)
