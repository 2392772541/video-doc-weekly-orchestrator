# v2输入与证据格式

模板只是格式，不是观测。**不能把示例时间、ID、true/成功字段作为真实结果提交。** 所有成功布尔值必须来自真实工具输出或Agent直接看过的源/页面，说明依据；只有测试fixture可以合成，且不得导入正式库。

## 文件与时效

- `run.json`由helper维护，不手改状态/哈希。`evidence/`存本期原始工具回执、源帧与最小必要截图。
- evidence_files是run内相对路径，必须在evidence目录；helper计算SHA256，变更后旧证据失效。
- 时间为带时区ISO8601，记实际观察时间。源视频记录size/mtime与时间点；飞书记录document_id+revision。
- 原作可访问证据有效期24h，写计划和导入全夹快照15min；两次写/两轮回读至少5秒。过期就重读，不改时间冒充新观察。
- BVID保持大小写；作品ID和收藏夹ID字符串，无Float/Number精度损失。canonical_url只能由verify产生。

## seed清单

`assets/manifest.example.json`含标题、来源周、numbered_item_count、quick_news_count、items与layout。
- items每项：key稳定且唯一，section/subsection/number/title/body/kind/url；kind=video|link|plain|quick_news；number可null；repeat_of为已证实同原作重复引用的首次key。
- layout为有序数组：heading(level,text)、paragraph(text或runs)、item(key)、image(owner_key,src,caption,evidence_files)。image必须在owner项之后，src是飞书真实图片token，证据至少包含源归属及上传/回读。seed中无已上传图时先保留图片待办，不宣称完整终稿。
- paragraph.runs支持text/href/bold，用于前言的直播/其它入口，不算作品收藏。
- 图片块在后续layout事件导入；helper由evidence_files计算evidence哈希。任何布局变动后重做inventory审查。

## apply事件（数组，按顺序执行）

| type | 必要字段 / 规则 |
|---|---|
| permissions | user_request真实原话/约定，publish布尔，collect平台数组 |
| register_source(video) | source_id,kind=video,path本地绝对路径,duration_seconds真实时长 |
| register_source(feishu) | source_id,kind=feishu,document_id,revision_id,checked_at,evidence_files |
| source | key,source_id,item_title,ownership=same_item,observed_url,author（源有才填）,timestamp_seconds或revision_id,evidence_files |
| item | key,changes；只可改title/body/section/subsection/number/kind/repeat_of，修改使旧来源核验失效 |
| add_item | item；用于补齐漏项，补后重排layout并inventory |
| layout | blocks完整有序布局 |
| inventory | expected_numbered,expected_quick_news,all_source_parts_reviewed,layout_reviewed,evidence_files |
| candidate | key,url,title,author,match_type,reason,evidence_files；不会变成已确认链 |
| verify | key,url,observed_title,observed_author,identity,availability,evidence_files |
| exception | key,user_request（实际接受缺失的原话）,reason,evidence_files；保持partial且排除收藏 |
| target | platform,account_id,authenticated,complete,checked_at,desired_name（可选）,folders,evidence_files |

verify.identity：method=source_url|observed_redirect|title_author_visual，same_work/title_match/reason必备；redirect还需redirect_from/to；恢复还需author_match/visual_match及来源作者，至少2份独立证据。

verify.availability：status=available、checked_at；视频observed_id必须完全一致。
- B站api：metadata.code=0,state=0,videos>0；值必须来自真实响应。
- 抖音api：business_code=0,detail_present=true。
- ui：detail_visible=true,unavailable_banner=false；实际查看页面。
- 网页http：http_status为实际2xx/3xx，content_matches=true且身份已核对，不能单凭200。

target.folders必须是完整发现的目标列表（或完整搜索结果），包含name和原始字符串id；抖音字段名改为collects_id_str。匹配期望名称的只能一项，可保存url。夹初次确实空才allow_empty_initial=true。

飞书target另有document_search_complete=true与same_issue_documents（0或1项，{id,title}）；多个同名文档会拦截而非自动清理。账号变化要真实account_change_user_request；有写尝试后换账号/夹需要新run。

## snapshot输入

参照 `assets/snapshot.example.json`。顶层：snapshot_id、method=api/ui、platform、account_id/folder_id/folder_name、started_at/finished_at、pages与evidence_files。

每页：request_cursor字符串、has_more布尔、next_cursor、items[{id}]。
- API每页必须business_code=0；UI每页必须target_visible、fully_loaded及evidence_files，末页end_visible。
- 中间页next_cursor与下一页request_cursor相同；末页has_more=false且next_cursor为空，缺页/循环不能验收。
- 列表失效壳无ID时可 `{unavailable:true,provider_id:真实平台原始字符串标识}`，不算目标ID。
- 同一snapshot_id不能重用；各轮时间真实递增，不把同一份数据复制两次。

## end-write输入

- acknowledged：`codes.favorite=0`，抖音还需`codes.folder_add=0`，evidence_files是真实API业务回执。
- ui_confirmed：不带codes；`ui={account_id,folder_id,id,favorite_visible:true,folder_selected_visible:true,reopened_detail:true}`，保存重新打开作品及目标夹状态的真实证据。
- uncertain/failed：detail写具体原因与evidence_files；后续操作被挡住，先fresh snapshot。

UI回执不代表全量验收完成；两轮全夹reconcile仍必需。

## publication输入

参照 `assets/publication.example.json`：account_id/folder_id、checked_at、document_file、folder_file、style_reviewed、style_evidence_files、document_url。
- document_file：真实 `ok/identity/data.document` 的全文fetch JSON；包含document_id/revision_id/content。
- folder_file：保留原始目录页证据后规范化为 `{complete,checked_at,account_id,folder_id,documents:[{id,title}]}`；不要猜“完整”。
- style_evidence_files：线上截图或完整格式结构的证据，验证视频黑字/非视频红粗与图片归属；render本地文件不是线上样式证据。

## 导出

`report/document.md`、`report/weekly.xml`：统一清单渲染。`audit/latest.json`、`audit/<platform>-reconcile.json`、`audit/delivery.json`：门禁与最终状态。`library.json/index.html/catalog.sqlite`：可查询来源、作品、收藏与候选。

schema是证据管理，不是密码学意义防作假系统；不得为了让脚本通过而伪造观测、认证、用户请求或完整性标志。
