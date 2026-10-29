【多智能体并行模式 - 最大线程】请用 Agent 工具并行派出多个子智能体完成 4 字科技公司名生成任务。

== 硬性要求 ==
1. 4个汉字，适合做科技公司名；
2. 寓意好，体现科技感/创新/探索/未来；
3. 好记，朗朗上口，不要太生僻；
4. 避开高频撞名词（华/鑫/盛/泰/恒/信/达/创/科/智/汇/源/丰/锐/融/锦/康/鼎/万/盈/宇/辰/跃/星河/东方等已被用滥的字）；
5. 风格、方向不限，自由发挥。

== 执行方式：最大并行 ==
第1棒：在【一条消息里】同时派出 3 个 Agent（并行），每个生成 6 个名字，共 18 个候选。各智能体自由发挥，不限方向。
每个智能体指令：'生成 6 个 4 字科技公司名，风格方向不限。要求简洁有科技感、寓意好、好记、避开高频撞名词、不要太生僻。给出【公司名】【拼音】【寓意】。只生成不检索。'

第2棒：拿到18个名字后，在【一条消息里】同时派出多个 Agent 并行严格检索（每个Agent查6个名字）。每个名字必须核查以下维度：
  - 公司注册：用 WebSearch 查是否已有同名公司注册（天眼查/企查查/企业信用信息公示系统）
  - 商标：用 WebSearch 查是否已有同名商标注册（商标局/中国商标网）
  - 域名：查 .com 和 .cn 域名是否可注册
每个名字输出完整结论：【公司名】【公司注册结论】【商标结论】【.com域名】【.cn域名】【综合判定：可用/不可用】

第3棒（主智能体）：去重 + 保存到 GitHub。严格按以下步骤执行：

步骤1 - 拉取索引和已有文件：
  执行命令获取 index.json（去重名单）：
    curl -s -H "Authorization: token YOUR_GITHUB_TOKEN" -H "Accept: application/vnd.github.v3.raw" "https://api.github.com/repos/cnshiyi/nameshiyi/contents/index.json"
  解析返回的 JSON，提取 names 数组（已存名字列表）。
  同时获取 候选.txt 和 状态.txt 的内容和 sha（用于后续更新）。

步骤2 - 过滤：
  从第2棒结果中筛选：综合判定为"可用" 且 不在 index.json 的 names 列表里。
  如果全部被过滤掉，直接结束，输出"本批无新名字可保存"。

步骤3 - 更新三个文件到 GitHub（必须全部完成，不能跳过）：

  文件A - 候选.txt（追加新名字）：
    在原有内容末尾追加，每行一个名字，格式：
    N. 公司名 - 拼音 - 寓意
    用 python3 拼接新旧内容，base64 编码后 PUT 到 GitHub。
    API: PUT https://api.github.com/repos/cnshiyi/nameshiyi/contents/候选.txt

  文件B - 状态.txt（追加检测结果）：
    在原有内容末尾追加本批次完整检测结果，格式：
    === 批次 2026-07-09 10:30 ===
    1. 公司名 | 公司注册:未注册 | 商标:无冲突 | .com:可注册 | .cn:可注册 | 综合:可用
    2. ...
    === 结束 ===
    API: PUT https://api.github.com/repos/cnshiyi/nameshiyi/contents/状态.txt

  文件C - index.json（追加去重名单）：
    把本批新名字追加到 names 数组中，更新 last_updated。
    API: PUT https://api.github.com/repos/cnshiyi/nameshiyi/contents/index.json

  具体操作模板（以候选.txt为例）：
    # 1. 获取当前文件内容和 sha
    RESP=$(curl -s -H "Authorization: token YOUR_GITHUB_TOKEN" "https://api.github.com/repos/cnshiyi/nameshiyi/contents/候选.txt")
    OLD_CONTENT=$(echo "$RESP" | python3 -c "import json,sys,base64;d=json.load(sys.stdin);print(base64.b64decode(d.get('content','')).decode() if d.get('content') else '')")
    SHA=$(echo "$RESP" | python3 -c "import json,sys;print(json.load(sys.stdin).get('sha',''))")
    # 2. 拼接新内容
    NEW_CONTENT="${OLD_CONTENT}\n1. 深度求索 - shēn dù qiú suǒ - 深耕底层技术，探求未知\n2. ..."
    # 3. base64 编码并推送
    ENCODED=$(python3 -c "import base64;print(base64.b64encode('''${NEW_CONTENT}'''.encode()).decode())")
    curl -s -X PUT -H "Authorization: token YOUR_GITHUB_TOKEN" -H "Accept: application/vnd.github.v3+json" -d "{\"message\":\"添加N个新名字\",\"content\":\"$ENCODED\",\"sha\":\"$SHA\",\"branch\":\"main\"}" "https://api.github.com/repos/cnshiyi/nameshiyi/contents/候选.txt"
  
  状态.txt 和 index.json 同理操作。

三棒顺序执行，但第1棒内部3个Agent并行、第2棒内部多个Agent并行。最终输出全部候选的检索结论表 + 本批保存了哪些新名字。
