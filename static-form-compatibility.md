# MechanicalSoup 静态网页表单采集的浏览器兼容边界分析

代码基线：本仓库 `mechanicalsoup/`（版本 1.5.0-dev，HEAD `385962b`），运行环境 bs4 4.12.3、requests 2.33.0，默认解析器 lxml。
文中所有"代码事实"均来自对 `form.py`、`browser.py`、`stateful_browser.py`、`utils.py` 的阅读，并辅以本地 HTML 通过 `Browser.get_request_kwargs` 构造真实 `requests.PreparedRequest` 得到的请求内容佐证（实验均为本地执行、无外网请求）。

## 1. 从 HTML 解析到请求发出的流水线

一次表单提交在代码中经过以下环节，每个环节都可能引入与浏览器的差异：

1. **解析**：`Browser.add_soup`（browser.py）按 Content-Type 或内容嗅探决定是否生成 `response.soup`，用 bs4 + lxml 解析。此时只得到静态 DOM，不执行任何脚本。
2. **选表**：`StatefulBrowser.select_form`（stateful_browser.py）按 CSS 选择器找到 `<form>`，然后做一件浏览器不会做的事——把全页面带 `form="<该表单id>"` 属性的元素**移动**（`form.extend(new_elements)`，bs4 的 append 语义是移动节点）到表单内部末尾。
3. **填表**：`Form.set_*`（form.py）在 `self.form` 子树内按 name 查找并改属性（`value`/`checked`/`selected`/textarea 文本）。此阶段不做任何禁用、校验检查。
4. **选提交按钮**：`Form.choose_submit` 在表单子树内收集 `input[type="submit" i]` 和 `button`（排除 `type=button/reset`），默认取第一个；`False` 表示不发送任何提交按钮（模拟 AJAX）。
5. **构造请求**：`Browser.get_request_kwargs`（browser.py）用选择器 `input[name],button[name],textarea[name],select[name]` 按（当前）文档顺序遍历表单子树，跳过 `is_disabled(tag)` 为真的控件（utils.py：自身 `disabled`，或祖先 `fieldset[disabled]` 且不在其第一个 `<legend>` 子元素内），按类型取值，组装成保序的 2 元组列表，GET 进 `params`、POST 进 `data`，multipart 走 `files`。
6. **发出**：交给 `requests.Session.request`。

字段归属（哪些控件算这张表单的）在第 2、5 步共同决定；请求顺序（name/value 对的排列）由第 2 步的移动和第 5 步的遍历顺序共同决定。这正是三类兼容问题的来源。

## 2. 三类机制的现状：规范预期 vs 代码事实

### 2.1 禁用字段集与 legend 例外控件 —— 已支持，且基本符合规范

`utils.is_disabled` 实现了 HTML 规范的 `concept-fieldset-disabled`：控件自身带 `disabled`，或它是某个 `fieldset[disabled]` 的后代、且不位于该 fieldset 的第一个 `<legend>` 子元素之内时，视为禁用，提交时跳过。

实验（本地 HTML：禁用 fieldset 内含 legend 内控件、普通 input、checkbox、select、textarea、button、嵌套未禁用 fieldset）：

```html
<form method="post" action="http://example.com/submit">
  <input name="enabled" value="1"/>
  <fieldset disabled>
    <legend><input name="in_legend" value="legend"/></legend>
    <input name="text" value="2"/>
    ...（checkbox/select/textarea/button/嵌套 fieldset 内 input）...
  </fieldset>
  <input name="after" value="9"/>
</form>
```

实际请求体：`enabled=1&in_legend=legend&after=9` —— 与规范预期完全一致：第一个 legend 内的控件豁免，嵌套 fieldset 不能"解除"祖先的禁用。第二个 `<legend>` 内的控件不豁免（实测请求体为空），也符合规范。

残留差异（源于当前处理，非规范分歧）：

- 禁用判断只在第 5 步（构造请求）生效。`Form.set_input` 等填表方法不检查禁用，可以对禁用控件赋值——赋值被静默接受但不出现在请求里。浏览器中用户根本无法与禁用控件交互，调用方若依赖"赋值即提交"会产生难以察觉的丢字段。
- `is_disabled` 依赖**祖先**关系。如下文 2.2 所示，外部归属机制会移动节点、改写祖先链，从而改变禁用判定的结果。

### 2.2 表单外部归属控件（`form=` 属性）—— 部分支持，差异源于当前处理

规范预期：带 `form="id"` 的可归属元素属于对应表单，无论它在文档中的位置；表单的 listed elements 按**文档树顺序**排列；反之，位于表单内部但 `form=` 指向别表的控件**不属于**该表单。

代码事实（`select_form` 的 `find_associated_elements` + `form.extend`）：

1. **归属是"移动"不是"关联"**。外部控件被物理移入 `<form>` 末尾，页面 DOM 被永久改写。实验：页面中 `<input form="f" name="ext_top">` 位于表单之前、`ext_area`/`ext_bottom`/`ext_sel` 位于表单之后，第一次 `select_form("#f")` 后它们全部变成 form 的子节点。
2. **请求顺序按元素类型分组，不是文档顺序**。`find_associated_elements` 按 `(input, button, fieldset, object, output, select, textarea)` 逐类 `find_all`，再整体追加到表单内控件之后。实测请求体：

   ```
   in1=1&in2=2&ext_top=T&ext_bottom=B&ext_sel=s&ext_area=mid
   ```

   规范预期（树顺序）应为 `ext_top=T&in1=1&in2=2&ext_area=mid&ext_bottom=B&ext_sel=s`。对把同名参数当有序列表解析的服务端（如 `?a=1&a=2` 的先后），这是可观察的差异。
3. **表单内部但 `form=` 指向别表的控件被错误提交**。`get_request_kwargs` 只按"后代 + 有 name"收集，不看 `form=` 属性。实验：`<input form="b" name="belongs_to_b">` 嵌在表单 A 内，提交 A 的请求体为 `a1=1&belongs_to_b=X`——规范要求它只属于 B。
4. **同一页面反复选择/改选表单，结果不稳定**。接上例：先 `select_form("#a")` 提交 A 得 `a1=1&belongs_to_b=X`；再 `select_form("#b")`（该控件被移入 B），提交 B 得 `b1=2&belongs_to_b=X`；**再次** `select_form("#a")` 提交 A 只剩 `a1=1`。同一张表单 A，两次提交字段集不同，完全取决于中间是否选过 B。这是"移动"语义的直接后果。
5. **移动会破坏禁用判定**。外部 `<fieldset form="f" disabled>` 包着 `<input form="f" name="ext_disabled">`：`select_form` 把 fieldset 和 input 作为两个独立节点分别移入表单，input 不再是 fieldset 的后代，`is_disabled` 返回 False，实测请求体 `in1=1&ext_disabled=D`——规范预期该控件被禁用、不提交。
6. **只有 StatefulBrowser 路径有归属**。直接用 `Browser.submit(form_tag)` 或手工构造 `Form` 时，外部控件完全不可见，`Form.set_*` 也找不到它们（抛 `LinkNotFoundError`）。两条 API 路径对同一页面的字段归属解释不一致。

### 2.3 提交按钮的覆盖行为 —— 部分支持

已支持（代码事实与规范一致）：

- 候选集：`input[type=submit]`（大小写不敏感）与 `<button>`（默认 type 即 submit；排除 `type=button/reset`），与规范的 submitter 定义基本吻合。
- 一次提交只发送一个 submitter；未指定时取第一个（`choose_submit(None)`），实测 `s1=one`；`choose_submit(False)` 则一个都不发（实测请求体为空），用于模拟 AJAX。
- `<button formaction>` 覆盖表单 `action`：实测 URL 变为 `http://example.com/button-override`，符合规范。
- 外部归属的提交按钮（`<button form="f">` 在表单外）经 `select_form` 移动后可被 `choose_submit` 选中并提交，实测 `x=1&ext_btn=E`。

差异（源于当前处理）：

- **`formaction` 只认 `<button>`，不认 `<input type=submit>`**。规范中两者都支持 `formaction`。实测 `<input type=submit formaction=".../input-override">` 的请求仍发往表单默认 `action`。
- **`formmethod` / `formenctype` 完全不支持**。实测 `<button formmethod="get">` 仍按表单的 `method=post` 发出，且数据在请求体而非 query string。
- **`input[type=image]` 不被识别为 submitter**，`choose_submit` 的候选选择器不含它；但它有 name，于是在 `get_request_kwargs` 里被当作普通字段提交为 `img=`（空值）。规范预期是点击时提交 `img.x=0&img.y=0`。当前行为两头不靠：既不能选为提交按钮，又会污染数据。
- `formnovalidate`、`formtarget` 无对应物（静态采集下 target 本就无意义，可接受）。

## 3. 规则分类汇总

**已支持（与规范一致，有测试覆盖）**：禁用属性与禁用 fieldset 的第一个 legend 例外（`tests/test_browser.py::test__request_disabled_fieldset`）；外部归属控件的基本收集（`tests/test_stateful_browser.py::test_select_form_associated_elements`）；单 submitter 语义与默认第一个；`<button formaction>`；radio/checkbox 仅提交 checked、缺省 value 为 `on`；select 无选中时取第一个 option、单选多选处理；同名多值保序（实测 GET `?z=1&a=2&z=3`）；GET/POST 数据分别进 params/data；multipart 文件上传。

**差异源于当前处理（静态即可确定，属实现缺口）**：外部控件按类型分组排序而非文档树顺序；归属用移动语义改写页面导致反复选择结果漂移；表单内 `form=` 指向别表的控件未排除；移动后祖先链改变使禁用判定失效；`formaction` 不覆盖 `input[type=submit]`；`formmethod`/`formenctype` 缺失；`input[type=image]` 既不可选又被当普通字段；填表 API 不检查禁用；StatefulBrowser 与裸 Browser 两条路径归属解释不一致。

**依赖脚本执行/用户交互后才能确定（静态采集原则上无法覆盖）**：提交前的约束校验（`required`、`pattern`、`type=email/number` 等，浏览器会阻止提交）；`onsubmit` 事件处理器与 `formdata` 事件对数据的改写；脚本动态增删的控件与动态修改的 `disabled`/`checked`；image 按钮的真实点击坐标；JS 计算出的 token/隐藏字段（如 CSRF 由脚本注入时）；提交按钮的"哪个被点"在自动化里本就由调用方指定，不算缺陷。

## 4. 兼容演进方案

### 4.1 目标语义

把字段归属与排序收敛到规范：表单的数据集 = 后代控件（排除 `form=` 指向别表者）∪ 文档中带 `form=<id>` 指向本表的控件，整体按**文档树顺序**排列；禁用判定在**原始页面 DOM** 上进行。这样"同一页面反复选择同一表单或改选另一表单"都有唯一确定的解释：归属与顺序只取决于页面本身，与选择历史无关。

### 4.2 具体改动与冲突处理

1. **归属改为非破坏性的"视图"**。`select_form` 不再 `extend` 移动节点，而是在 `Form` 上记录"页面引用 + 表单 id"，`get_request_kwargs` 遍历时按文档顺序合并内部后代与外部归属控件，并在遍历时排除 `form=` 指向别表的后代。禁用判定基于原始祖先链，移动问题随之消失。
   - 与现有结果的冲突：凡是页面含 `form=` 外部控件、或表单内嵌 `form=` 指向别表控件的采集，字段集合和顺序都会变。这类页面此前结果本身就不稳定（见 2.2 第 4 点），变更属于修正而非回归。
2. **提交按钮覆盖补全**：`formaction` 支持扩展到 `input[type=submit]`；新增 `formmethod`（get/post 两者）；`formenctype` 映射到 multipart 开关；`input[type=image]` 纳入 submitter 候选，选中时提交 `name.x=0&name.y=0`（静态场景坐标取 0,0，是无坐标信息时的合理默认），未选中时**不**再作为普通字段出现在数据里。
   - 与现有结果的冲突：含 `input[type=image] name=...` 的表单，旧行为多发一个 `img=` 空字段，修正后消失；依赖这个空字段的服务端交互会变化。`formaction` 扩展只影响此前被静默忽略覆盖的表单，旧行为明确是错的。
3. **填表 API 增加禁用可见性**：`Form.set_*` 对禁用控件默认抛出或警告（可选参数关闭），避免"赋值成功但未提交"的静默丢字段。

### 4.3 对既有调用的影响面与迁移选择

受影响的既有调用仅限于页面使用了以下特性的采集任务：外部 `form=` 归属控件、表单内嵌跨表 `form=` 控件、`input[type=submit formaction]`、`formmethod`/`formenctype`、`input[type=image]`、对禁用控件赋值。普通表单（控件全在表单内、无覆盖属性）的请求逐字节不变，占绝大多数存量用法。

迁移选择（保留旧结果的通道）：

- **版本开关**：`StatefulBrowser(compat="1.x")` 或模块级设置，走旧的移动式归属与旧 submitter 逻辑，行为与当前完全一致；默认新语义。开关作用于 `select_form`/`Form` 构造点，同一进程内可混用。
- **显式冻结**：调用方可用 `Browser.get_request_kwargs` 在当前版本生成一次请求参数并缓存，升级后改用 `session.request(**缓存参数)` 重放，逐字节保留旧结果，不依赖库保留旧代码路径。
- **失败时的可观察结果**：新语义下无法归类的输入必须显式失败而非静默变化——`choose_submit` 找不到/多义继续抛 `LinkNotFoundError`；`form=` 指向不存在的表单 id 时给出 `warnings.warn`；对禁用控件赋值在新默认下抛 `LinkNotFoundError` 子类。所有行为变化点（字段集合变化、顺序变化、URL 被 formaction 改写）在 `verbose>=2` 或 `logging` 通道输出 diff 级日志，使升级后请求与旧结果的差异可直接比对。

### 4.4 动态页面输入的责任边界

静态采集对依赖脚本执行的输入采取**接受原样、不模拟**的立场：HTML 里写什么就采集什么，约束校验、事件处理器、动态控件一律不执行、不推断。具体而言：

- 接受：静态 HTML 中已存在的全部控件与属性（含 `required` 等校验属性——采集照发，是否被服务端拒绝由服务端决定）。
- 拒绝（明确不支持、不尝试）：需要执行 JS 才能确定的字段值、动态插入的控件、事件改写的数据。这类输入不属于静态采集的语义范围，库不应猜测。
- 调用方责任：识别目标页面是否依赖脚本（例如对比 `browser.page` 与预期字段、捕获 `LinkNotFoundError`）；若依赖，改用真实浏览器（Selenium/Playwright）渲染后，可把渲染后的 HTML 交回 MechanicalSoup 处理，或直接用 `Browser.post` 手工构造请求。`choose_submit(False)` 已是为此类"绕过表单、自造请求"场景预留的出口，文档中应把它与动态页面的关系写清楚。

## 附录：实验方法

所有实验均为本地执行：用 `StatefulBrowser.open_fake_page(html, url=...)` 或 `BeautifulSoup(html, "lxml").form` 构造页面，调用 `Browser.get_request_kwargs(...)` 得到 `method/url/params/data`，再经 `requests.Session().prepare_request` 生成 `PreparedRequest`，打印最终方法与 URL、请求体。无外网请求。关键实测输出已内联在第 2 节；对应现象（外部控件被移入表单、请求体字段与顺序、formaction 是否生效等）均可按上述方法复现。仓库既有测试 `test__request_disabled_fieldset`、`test_select_form_associated_elements`、`test_get_request_kwargs_many_submit_elements_have_formaction_attribute` 分别覆盖了 2.1、2.2 基本路径与 2.3 的 button formaction，可作为演进时的回归基线。
