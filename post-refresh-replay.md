# MechanicalSoup 表单 POST 后刷新/重放的重复写入风险分析

分析对象：本仓库 `mechanicalsoup/`（`StatefulBrowser` + `Browser`），运行环境
Python 3.12.4、requests 2.33.0。实测脚本为 `exp_post_refresh.py`，完整输出见
`exp_output.txt`。下文"实测结论"与"源码推导"分开陈述；凡涉及"是否安全"的
判断，均在末尾标明其对服务端幂等约束的依赖。

## 1. 实测方法

本地 `ThreadingHTTPServer` 提供计数端点，记录每个请求的序号、方法、路径、
Cookie 头和请求体；所有被服务端接受的写入追加到 `WRITES` 列表：

| 端点 | 行为 |
|---|---|
| `GET /form` | `Set-Cookie: sid=FORM-1`，返回 4 个表单 |
| `POST /submit-direct` | 计数写入，`Set-Cookie: sid=AFTER-DIRECT`，直接返回 200 HTML |
| `POST /submit-303` | 计数写入，`Set-Cookie: sid=AFTER-303`，303 → `GET /result303` |
| `POST /submit-307` | 不写入，`Set-Cookie: sid=AFTER-307`，307 → `POST /write307`（在目标端计数写入） |
| `POST /submit-drop` | 计数写入后**不返回任何响应直接断开连接**，模拟"响应丢失" |

客户端每次操作后快照三项状态并与服务端日志对齐：`browser.url`（当前页面
地址）、`__state.request`（浏览器保存的 PreparedRequest，含方法/URL/Cookie
头/请求体）、`session.cookies`（当前会话 Cookie 罐）。

## 2. 实测结论

### A. POST 直接返回 200 HTML：刷新 = 静默重复提交

提交后状态快照：

- `browser.url = http://127.0.0.1:PORT/submit-direct`
- 保存的请求：`POST /submit-direct`，`Cookie: sid=FORM-1`，body `item=widget`
- 会话 Cookie 罐：`sid=AFTER-DIRECT`（已被响应更新）

`refresh()` 后服务端实际收到第二条 `POST /submit-direct`，body 完全相同，
写入计数从 1 变为 2：

```
seq=2 POST /submit-direct cookie=sid=FORM-1 body=item=widget [write]
seq=3 POST /submit-direct cookie=sid=FORM-1 body=item=widget [write]
```

注意刷新发出的 Cookie 是**旧的 `FORM-1`**，不是罐中当前的 `AFTER-DIRECT`
（冲突分析见第 4 节）。与真实浏览器"确认重新提交表单"的交互不同，
MechanicalSoup 的刷新**无任何确认、静默重放 POST**。

### B. POST → 303 → GET：刷新安全（Post/Redirect/Get 模式生效）

提交后保存的请求是重定向链的**最终请求** `GET /result303`（无 body，
`Cookie: sid=AFTER-303`），`browser.url` 也是 `/result303`。`refresh()`
只重放该 GET，写入增量为 0：

```
seq=6 GET /result303 cookie=sid=AFTER-303 [result]
seq=7 GET /result303 cookie=sid=AFTER-303 [result]   <-- refresh，无新写入
```

302 与 303 在 requests 中走同一条 `rebuild_method` 路径（POST 改写为 GET、
丢弃 body），结论相同。

### C. POST → 307 → POST：刷新仍然重复写入

307 保留方法和请求体，重定向链的最终请求仍是 `POST /write307`，
body `item=widget`。提交后保存的请求就是这个 POST，`refresh()` 原样重放，
写入增量为 1：

```
seq=10 POST /write307 cookie=sid=AFTER-307 body=item=widget [write]
seq=11 POST /write307 cookie=sid=AFTER-307 body=item=widget [write]  <-- refresh
```

即：**是否重复写入不取决于"是否重定向"，而取决于重定向后最终请求的方法**。
303/302 把 POST 降级为 GET 从而偶然保护了刷新；307/308 和直接 200 一样危险。

### D. 重新提交已选表单 = 重新执行最初的表单提交

保留 `select_form` 返回的 `Form` 对象，用 `Browser.submit(br, form=form,
url=...)` 再次提交（303 端点）：服务端收到一条**新的** `POST /submit-303`
（随后跟随 303 的 GET），写入增量为 1。与刷新有两点本质区别：

1. 这是**重新执行最初表单提交**（从 Form 对象重新构造请求），而刷新是
   **重放已保存的最终请求**；
2. 重新提交走 `session.request()`，会合并**当前** Cookie 罐——实测发出的是
   `Cookie: sid=AFTER-303`；而刷新发的是保存请求里冻结的旧 Cookie。

### E. `submit_selected(update_state=False)`：状态与写入脱钩

提交确实产生了写入（`seq=18 POST /submit-direct [write]`），但浏览器状态
仍停留在 `GET /form`：保存的请求是打开表单页时的 GET，`browser.url` 仍是
`/form`。随后的 `refresh()` 重放的是该 GET，写入增量为 0。即
`update_state=False` 下刷新与刚才的提交完全脱钩——既不会重复写入，也不
反映提交后的页面。

附带观察：这次 `GET /form` 重放**完全没有带 Cookie**（`cookie=None`），
尽管罐中已有 `AFTER-DIRECT`——因为保存的 PreparedRequest 是在收到任何
`Set-Cookie` 之前制备的（见第 4 节）。

### F. 响应丢失但服务端已接受写入：客户端证据不足以支持安全重试

`POST /submit-drop` 在服务端计数写入后直接断开连接。客户端只收到
`requests.exceptions.ConnectionError`，`submit_selected` 抛异常、状态不
更新（仍停留在 `GET /form`，保存的请求仍是该 GET）。服务端写入增量为 1。

客户端此时持有的全部证据是"一个连接异常"，它**无法区分**至少三种情形：
请求未到达、到达但未处理、已处理但响应丢失。MechanicalSoup 和 requests
都没有为表单提交生成幂等键或去重令牌，也没有"上次提交是否落库"的任何
记录。因此**仅凭客户端现有证据不能安全自动重试**：重试在情形三下就是
重复写入（本实验 A/C 已演示重放必然产生第二次写入），不重试在情形一、
二下就是丢失写入。能否安全重试完全取决于服务端是否提供幂等语义
（见第 7 节）。

### G. `close()` 之后：状态可读但不可刷新

`close()` 清空 Cookie 罐并把 `session` 置为 `None`，但 `__state` 不动：
`browser.page`、`browser.url` 仍可读取最后页面。`refresh()` 因
`self.session.send` 变为 `None.send` 抛出
`AttributeError: 'NoneType' object has no attribute 'send'`。即关闭后
"页面状态"（page/url/form 快照）与"网络能力"（session）解耦：快照仍可
观察，任何刷新/提交都不再可能，重复写入风险随 session 销毁而消失。

## 3. 源码推导

以下结论由源码直接推出，与上面实测一一对应。

**状态模型。** `stateful_browser.py` 中 `_BrowserState` 保存四元组
`(page, url, form, request)`。`open()`（L153-167）、`submit_selected()`
（L295-326，`update_state=True` 时）都用
`_BrowserState(page=resp.soup, url=resp.url, request=resp.request)` 整体
替换状态。`resp.request` 是 requests 响应对象上**重定向链最后一个请求的
PreparedRequest**——这解释了为什么 303 后保存的是 GET 而 307/直接 200
后保存的是 POST。

**刷新即重放。** `refresh()`（L188-207）核心只有一行网络操作：

```python
resp = self.session.send(old_request)   # L203
```

它把保存的 PreparedRequest **原样**交给 `Session.send`。requests 2.33 的
`Session.send` 对 PreparedRequest 不做任何重新制备——**不会**把会话
Cookie 罐合并进请求（合并只发生在 `Session.request` → `prepare_request`
阶段）。因此重放请求的方法、URL、body、Cookie 头全部冻结在当初制备的
那一刻。`Session.send` 默认 `allow_redirects=True`，所以刷新仍会跟随新的
重定向，且重定向跳转请求**会**通过 `resolve_redirects` 里的
`merge_cookies(prepared_request._cookies, self.cookies)` 合并当前 Cookie
罐——只有重放的第一跳使用旧 Cookie。

**表单提交是重新构造。** `Browser.submit`（browser.py L368-407）→
`_request` → `get_request_kwargs`（L244-363）每次从 Form 对象遍历控件、
重新拼 data，再走 `session.request()`，因此使用当前 Cookie 罐和当前会话
配置。这就是"重放最终请求"与"重新执行最初表单提交"在源码上的分界线：
前者是 `session.send(保存的 PreparedRequest)`，后者是
`session.request(**重新构造的 kwargs)`。

**重定向方法改写。** requests 的 `rebuild_method`：301/302/303 把 POST
改写为 GET 并丢弃 body；307/308 保留方法和 body（要求 body 可回绕，
`application/x-www-form-urlencoded` 的字符串 body 满足）。这决定了保存的
"最终请求"是否仍是写操作。

**`update_state=False` 与 `close()`。** `submit_selected` 仅在
`update_state=True` 时替换 `__state`（L323-326）；为 False 时状态仍指向
上一个页面，刷新重放的是上一个页面的请求。`Browser.close()`
（browser.py L402-406）执行 `session.cookies.clear()`、`session.close()`、
`session = None`，但不触碰 `__state`，故页面快照可读、`refresh()` 抛
`AttributeError`。

**异常路径。** `submit_selected` 中状态替换发生在 `Browser.submit` 返回
**之后**；`session.request` 抛 `ConnectionError` 时状态保持提交前的页面。
客户端侧没有任何重试、去重或幂等键逻辑（requests 默认
`Retry(total=0)`，且即使配置 urllib3 Retry，POST 默认也不在幂等重试
白名单内）。

## 4. 刷新沿用已保存请求与当前 Cookie 的冲突

实测 F 给出了冲突的完整证据链：

```
jar 当前值:            sid=AFTER-DIRECT
保存请求的 Cookie 头:  sid=FORM-1
refresh 实际发出:      Cookie: sid=FORM-1   <-- 旧值
```

冲突根源：`refresh()` 重放的 PreparedRequest 在**制备那一刻**就固化了
Cookie 头，而 `Session.send` 不会用当前 Cookie 罐刷新它。后果分两个方向：

- **会话已前进**（服务端在 POST 响应里轮换了会话标识，如本例）：刷新带着
  过期会话去重放写操作。若服务端校验会话，可能拒绝这次重放（偶然起到
  保护作用）；若服务端按 body 处理，则在新会话上下文外重复写入，产生
  归属错乱的数据。
- **会话已失效/登出**：刷新仍带着旧 Cookie 重放 POST，可能写入到一个
  本应已终止的会话，或服务端返回登录页而客户端状态被替换成登录页
  （`refresh` 无条件用响应替换 `__state`），原页面状态丢失。
- 反向地，实验 E 显示保存于 Set-Cookie 之前的请求重放时**完全不带
  Cookie**，即使罐中已有有效会话——"刷新"和"当前会话"在两个方向上都
  可能不一致。

只有重放的第一跳使用冻结 Cookie；若第一跳得到重定向，后续跳转请求会
合并当前 Cookie 罐（`resolve_redirects` 的 `merge_cookies`），于是同一次
刷新内不同跳可能属于不同会话状态，进一步加剧不可预期性。

## 5. 保留历史刷新行为的可选保护方案

`refresh()` 重放已保存请求是既有语义（文档明确"以最初相同的请求重新
加载当前页面"），不宜直接改变。可在其上叠加可选保护：

1. **非幂等方法守卫（默认关闭或默认开启均可选）**：`refresh()` 前检查
   `__state.request.method`，若为 POST/PUT/PATCH/DELETE 等写方法则拒绝
   自动重放，抛出专门异常（如 `ResubmitRequiredError`）或返回哨兵。
   拒绝后页面状态完全不动——`page`/`url`/`form`/保存的请求全部保持
   拒绝前的值，调用方仍可读取当前页面、继续 `select_form` 或导航离开；
   这与网络失败后的可观察性一致（实验 G：异常后状态停留在提交前页面，
   可继续操作）。
2. **调用方确认后再发送的边界**：确认动作应是**显式的新调用**（如
   `refresh(force=True)` 或重新 `submit_selected`），而不是静默重试。
   边界建议定为：确认后允许**恰好一次**重放；重放结果照常替换状态；
   若重放再次失败，回到第 1 条的拒绝路径，不自动连发。确认语义应
   文档化为"我接受服务端可能已处理过该请求、可能产生重复写入"。
3. **刷新前重校验 Cookie**：可选地在重放前用当前 Cookie 罐重新制备
   请求（等价于 `session.prepare_request(session.request(...))` 按保存
   请求的方法/URL/body 重建），消除第 4 节的旧 Cookie 冲突；但应注意
   这会改变"原样重放"的历史语义，宜作为独立选项。
4. **拒绝自动重放的明确条件**（满足任一即拒绝）：
   - 保存请求的方法是非幂等写方法（POST/PUT/PATCH/DELETE）；
   - 保存请求缺失（`__state.request is None`，现有 `ValueError` 路径，
     应保留）；
   - 上一次提交以网络异常告终（客户端无法确认服务端是否已写入，
     见实验 G）；
   - session 已关闭（`session is None`，目前表现为 `AttributeError`，
     宜改为显式异常）。
5. **幂等键透传**：允许调用方在表单中加入一次性令牌隐藏域（或服务端
   提供的 idempotency-key 头），把去重责任显式交给服务端；客户端只
   负责"重放时带上同一个令牌"，不负责判断能否重放。

## 6. 网络失败/拒绝后的可观察状态

两类"未成功"路径下，客户端状态都是**自洽且可继续观察**的：

- 网络失败（实验 G）：`submit_selected` 抛异常，状态仍是提交前的页面
  （`/form`，保存的请求是该 GET）。调用方可读 `page` 检查表单、可
  `select_form` 重新提交（= 重新执行最初提交，用当前 Cookie）、可
  `refresh()`（重放 GET，安全）。但没有任何客户端状态能回答"刚才那次
  POST 服务端写没写"。
- 守卫拒绝（建议方案）：状态不变，与拒绝前完全一致，调用方持有全部
  上下文来做确认决策。
- `close()` 之后（实验 H）：`page`/`url` 仍可读最后快照，session 为
  `None`，任何刷新/提交都会失败——这是终态，不再存在重复写入面。

## 7. 哪些结论依赖服务端幂等约束

以下客户端机制事实**不依赖**服务端行为，由源码和实测直接成立：

- 刷新重放的是保存的最终请求；直接 200 与 307 下该请求是带 body 的
  POST，303/302 下是 GET；
- 重放使用冻结 Cookie，与当前 Cookie 罐可能不一致；
- 重新提交已选表单总是产生一次新的完整提交；
- 响应丢失时客户端只有异常、无任何落库证据；
- `update_state=False` 与 `close()` 对状态/会话的影响。

以下**安全性**结论依赖服务端幂等约束，客户端单方无法保证：

- "重复写入是否真的有害"：仅当服务端对重复 POST 不去重（无唯一约束、
  无幂等键、无 CSRF 一次性令牌）时，刷新重放才表现为重复数据；本实验
  的计数端点正是无幂等保护的最坏情形。
- "响应丢失后能否安全重试"：仅当服务端提供幂等语义（幂等键去重、
  唯一约束冲突可识别、或该写操作天然幂等）时，重试才是安全的；否则
  客户端必须在"可能重复"与"可能丢失"之间由调用方业务层裁决。
- "旧 Cookie 重放是否被服务端拒绝"：取决于服务端是否校验会话新鲜度；
  校验则偶然阻断重放，不校验则静默接受重复写入。
- "303 模式刷新安全"：依赖服务端把写操作放在 303 之前且 GET 目标无
  副作用；若 GET 目标本身有副作用，刷新重放 GET 同样不安全。
