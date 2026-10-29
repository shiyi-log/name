"""名字评分：把类别查表式的打分换成连续特征打分。

设计原则
--------
1. 每个维度都由多个连续信号加权得到，不再依赖单一类别判断。
2. 读音维度真正使用拼音：声调走势、同音字、声母韵母重复。
3. 分数保留一位小数，避免三万多条候选挤在十几个整数上。
4. 所有扣分项都写进 reasons，让"为什么低"同样可见。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field

from pypinyin import Style, pinyin

# ---------------------------------------------------------------------------
# 权重：与 README / 前端保持一致
# ---------------------------------------------------------------------------

SCORE_RULES = [
    ("brand", "品牌感", 28, "是否有动作、方向或可延展的品牌叙事"),
    ("readability", "读写与韵律", 20, "是否顺口、易读、四字节奏是否自然"),
    ("poetry", "诗意与意象", 20, "是否有画面感，同时避免直接照搬成语"),
    ("distinctiveness", "独特性", 18, "词组组合是否有辨识度，是否容易记住"),
    ("registration", "注册启发分", 14, "基于词组常见度的启发式低冲突评分，不等于工商或商标核验"),
]

WEIGHTS = {key: weight for key, _, weight, _ in SCORE_RULES}

DIMENSION_LABELS = {key: label for key, label, _, _ in SCORE_RULES}


# ---------------------------------------------------------------------------
# 字表
# ---------------------------------------------------------------------------

# 强动作字：放在首位时最能带出"正在发生"的品牌动势
STRONG_MOTION = set("见破启行开探寻觅逐溯入向问取成破扬聚融联驱跃越连派")

# 弱动作字：有方向感，但动词性偏弱
WEAK_MOTION = set("听观知守求思照归渡乘风拾")

# 意象字：具体、可成像
SENSORY_CHARS = set("云星江溪山竹远清青长明月光野花影烟潮雾霁岚风水寒春暮夏秋冬夜晨晓夕阳雷雪霜虹玉舟渡途川河海深空天镜林柳松苍沧浮流漫隐照辉黎明寰宇")

# 季节与天象：让名字自带时间感
SEASON_CHARS = set("春夏秋冬晨晓夕暮夜")

# 收尾处最能撑开想象空间的抽象词
OPEN_ENDINGS = {"远方", "远志", "无界", "未央", "长远", "开元", "开源", "有光", "有道", "有序", "有恒", "有信", "有度", "有方", "有为", "自在", "远见"}

# ---------------------------------------------------------------------------
# 常见四字固定搭配：命中即说明"不像公司名，像现成词"
# ---------------------------------------------------------------------------

IDIOMS = {
    "开物成务", "乘风破浪", "知行合一", "见微知著", "守正创新", "行远自迩",
    "拨云见日", "柳暗花明", "水到渠成", "一叶知秋", "见贤思齐", "聚沙成塔",
    "集腋成裘", "融会贯通", "推陈出新", "温故知新", "返璞归真", "化险为夷",
    "登高望远", "高瞻远瞩", "厚积薄发", "水滴石穿", "绳锯木断", "积少成多",
    "开源节流", "开源创新", "守正出新", "正本清源", "源远流长", "根深叶茂",
    "春华秋实", "秋收冬藏", "云开见日", "云开雾散", "拨云见天", "拨开云雾",
    "风和日丽", "清风明月", "明月清风", "山高水长", "山长水远", "流水行云",
    "行云流水", "云淡风轻", "风轻云淡", "海阔天空", "天高海阔", "海纳百川",
    "百川归海", "万流归宗", "星火燎原", "星星之火", "薪火相传", "生生不息",
    "日新月异", "与时俱进", "日积月累", "久久为功", "持之以恒", "锲而不舍",
    "跬步千里", "千里之行", "一以贯之", "慎终如始", "善始善终", "专心致志",
    "精诚所至", "金石为开", "柳暗花明", "峰回路转", "豁然开朗", "拨云见月",
    "明心见性", "返本开新", "继往开来", "承前启后", "古为今用", "洋为中用",
    "博采众长", "兼容并蓄", "兼收并蓄", "海纳百川", "有容乃大", "厚德载物",
    "自强不息", "止于至善", "修身齐家", "格物致知", "诚意正心", "正心诚意",
    "明德惟馨", "明德至善", "惟精惟一", "允执厥中", "执中守正", "中正平和",
    "光风霁月", "霁月光风", "春风化雨", "化雨春风", "润物无声", "潜移默化",
    "水滴石穿", "铁杵成针", "持之以恒", "永不言弃", "坚定不移", "矢志不渝",
    "不忘初心", "牢记使命", "砥砺前行", "笃行致远", "行稳致远", "宁静致远",
    "高飞远举", "展翅高飞", "鹏程万里", "大展宏图", "鸿图大展", "宏图大展",
    "蒸蒸日上", "欣欣向荣", "方兴未艾", "蓬勃发展", "茁壮成长", "根深蒂固",
    "枝繁叶茂", "开枝散叶", "薪尽火传", "衣钵相传", "一脉相承", "源远流长",
    "生生不已", "周而复始", "循环往复", "生生不息", "绵延不绝", "川流不息",
    "奔流不息", "滔滔不绝", "源远流长", "沧海桑田", "沧海一粟", "斗转星移",
    "物换星移", "时过境迁", "岁月如流", "光阴似箭", "白驹过隙", "未央长夜",
    "长夜未央", "夜以继日", "日以继夜", "通宵达旦", "彻夜不眠", "秉烛夜游",
    "秉烛达旦", "挑灯夜战", "灯火通明", "万家灯火", "人间烟火", "烟火人间",
    "晨钟暮鼓", "暮鼓晨钟", "朝夕相处", "只争朝夕", "分秒必争", "争分夺秒",
    "时不我待", "岁月不居", "时节如流", "春去秋来", "寒来暑往", "秋去春来",
    "四季轮回", "周行不殆", "天道酬勤", "地道酬善", "人道酬诚", "商道酬信",
    "业道酬精", "臻于至善", "精益求精", "尽善尽美", "登峰造极", "炉火纯青",
    "出神入化", "游刃有余", "得心应手", "水乳交融", "融为一体", "浑然一体",
    "浑然天成", "巧夺天工", "独具匠心", "别具一格", "独树一帜", "别开生面",
    "耳目一新", "焕然一新", "面目一新", "气象一新", "万象更新", "革故鼎新",
    "吐故纳新", "推陈致新", "除旧布新", "标新立异", "匠心独运", "开天辟地",
    "愚公移山", "精卫填海", "夸父逐日", "嫦娥奔月", "女娲补天", "后羿射日",
    "开天辟地", "改天换地", "经天纬地", "顶天立地", "铺天盖地", "遮天蔽日",
    "气吞山河", "气壮山河", "山河无恙", "国泰民安", "风调雨顺", "五谷丰登",
    "六畜兴旺", "歌舞升平", "太平盛世", "海晏河清", "河清海晏", "政通人和",
    "百废俱兴", "万象更新", "安居乐业", "丰衣足食", "繁荣昌盛", "兴旺发达",
    "前程似锦", "前途无量", "光明磊落", "光风霁月", "高风亮节", "虚怀若谷",
    "上善若水", "厚德载福", "知足常乐", "随遇而安", "顺其自然", "自然而然",
    "水到渠成", "瓜熟蒂落", "顺理成章", "理所当然", "天经地义", "名正言顺",
    "有理有据", "有条不紊", "井然有序", "循序渐进", "按部就班", "一步一个",
    "脚踏实地", "稳扎稳打", "步步为营", "环环相扣", "丝丝入扣", "严丝合缝",
    "天衣无缝", "滴水不漏", "无懈可击", "尽善尽美", "完美无缺", "十全十美",
    "精益求精", "锦上添花", "如虎添翼", "雪中送炭", "画龙点睛", "点石成金",
    "化腐朽为", "妙手回春", "起死回生", "枯木逢春", "柳暗花明", "否极泰来",
    "苦尽甘来", "雨过天晴", "云开日出", "拨云见日", "豁然开朗", "峰回路转",
    "山重水复", "船到桥头", "车到山前", "水落石出", "真相大白", "一目了然",
    "一清二楚", "了如指掌", "洞若观火", "明察秋毫", "见微知著", "一叶知秋",
    "管中窥豹", "可见一斑", "由表及里", "由此及彼", "举一反三", "触类旁通",
    "融会贯通", "学以致用", "知行合一", "言行一致", "表里如一", "始终如一",
    "一如既往", "一以贯之", "贯彻始终", "善始善终", "有始有终", "有头有尾",
    "善作善成", "久久为功", "功不唐捐", "水到渠成", "瓜熟蒂落", "顺理成章",
}

# 常见品牌/公司名结构词：命中说明"像同行"，注册冲突风险更高
BRAND_CLICHES = {
    "科技", "智能", "数字", "网络", "互联", "未来", "创新", "创想", "创投",
    "集团", "控股", "实业", "发展", "文化", "传媒", "教育", "健康", "环保",
    "能源", "生物", "医药", "地产", "建设", "工程", "商贸", "咨询", "服务",
}

# 生硬拼接：语义上无法成像的组合信号
INCOHERENT_BIGRAMS = {
    "有序共生", "知行起势", "正道同行", "有信成势", "同心合流",
}



# ---------------------------------------------------------------------------
# 拼音特征
# ---------------------------------------------------------------------------

def pinyin_profile(name: str) -> dict:
    """一次解析出四个字的三套读音特征。"""
    syllables = pinyin(name, style=Style.TONE3, neutral_tone_with_five=True)
    initials = pinyin(name, style=Style.INITIALS, strict=False)
    finals = pinyin(name, style=Style.FINALS, strict=False)
    plain = pinyin(name, style=Style.NORMAL)

    tones: list[int] = []
    has_tone = pinyin(name, style=Style.TONE3, neutral_tone_with_five=True)
    for item in has_tone:
        token = item[0]
        match = re.search(r"([1-5])$", token)
        tones.append(int(match.group(1)) if match else 0)

    return {
        "tones": tones,
        "initials": [i[0] for i in initials],
        "finals": [f[0] for f in finals],
        "plain": [p[0] for p in plain],
        "syllables": [s[0] for s in syllables],
    }


def tone_curve_score(tones: list[int]) -> tuple[float, list[str]]:
    """声调走势：四声有起伏最顺口，全同调最拗口。

    普通话里一二声为平、三四声为仄。理想节奏是平仄交替，
    四字全平或全仄读起来像口号，缺少顿挫。
    """
    notes: list[str] = []
    score = 7.0
    if not tones or len(tones) != 4:
        return score, notes

    unique = len(set(tones))
    if unique == 4:
        score += 1.6
        notes.append("四声调各不相同，读起来有起伏。")
    elif unique == 3:
        score += 1.0
    elif unique == 2:
        score -= 0.4
    else:
        score -= 2.2
        notes.append("四个字同调，读起来平直，缺少顿挫。")

    # 三连相同声调
    for start in range(2):
        if tones[start] == tones[start + 1] == tones[start + 2]:
            score -= 0.9
            notes.append("连续三个字同调，节奏偏平。")
            break

    # 平仄交替
    level = [1 if tone in (1, 2) else 0 for tone in tones]
    alternations = sum(1 for i in range(3) if level[i] != level[i + 1])
    if alternations == 3:
        score += 1.2
        notes.append("平仄交替完整，四字节奏最自然。")
    elif alternations == 2:
        score += 0.5
    elif alternations == 0:
        score -= 1.0
        notes.append("全平或全仄，朗读时缺少对比。")

    return score, notes


def phonetic_repetition_score(profile: dict) -> tuple[float, list[str]]:
    """声母、韵母、整音节的重复都会让名字拗口或含混。"""
    notes: list[str] = []
    score = 0.0

    plain = profile["plain"]
    duplicates = [item for item, count in Counter(plain).items() if count > 1]
    if duplicates:
        score -= 1.4 * len(duplicates)
        notes.append("含同音字，口头传播容易听混。")

    initials = [i for i in profile["initials"] if i]
    if len(initials) >= 3 and len(set(initials)) <= 2:
        score -= 0.8
        notes.append("声母重复过多，读起来黏连。")

    finals = [f for f in profile["finals"] if f]
    if len(finals) >= 3 and len(set(finals)) == 1:
        score -= 1.0
        notes.append("四字韵母相同，尾音拖沓。")

    return score, notes


# ---------------------------------------------------------------------------
# 词表驱动的语义特征
# ---------------------------------------------------------------------------

def idiom_hit(name: str) -> bool:
    return name in IDIOMS


def cliche_hit(name: str) -> bool:
    return any(word in name for word in BRAND_CLICHES)


# ---------------------------------------------------------------------------
# 候选池特征（需要在生成时统计）
# ---------------------------------------------------------------------------

@dataclass
class CorpusStats:
    """整个候选池的分布，用于计算相对稀有度。

    稀有度按"出现次数在全部不同次数中的分位"计算，而不是按次数的对数。
    对数是长尾分布，三万多条候选里绝大多数会挤在 0.5 附近，
    导致稀有度这一项几乎没有区分力。分位法能把分布真正摊开。
    """

    bigram: Counter = field(default_factory=Counter)
    char: Counter = field(default_factory=Counter)
    token: Counter = field(default_factory=Counter)
    token_pair: Counter = field(default_factory=Counter)
    # 字在 8105 字表中的序号，越小越常用。8105 字表本身按字频排序，
    # 这是判断"用了多常见的字"最可靠的一个外部信号。
    char_rank: dict[str, int] = field(default_factory=dict)
    total: int = 0
    _ranks: dict[int, dict[int, float]] = field(default_factory=dict)

    def _rank_table(self, counter_id: int, counter: Counter) -> dict[int, float]:
        if counter_id in self._ranks:
            return self._ranks[counter_id]
        distinct = sorted(set(counter.values()))
        if len(distinct) <= 1:
            table = {count: 1.0 for count in distinct}
        else:
            span = len(distinct) - 1
            table = {count: 1.0 - position / span for position, count in enumerate(distinct)}
        self._ranks[counter_id] = table
        return table

    def rarity(self, counter: Counter, key: str, counter_id: int) -> float:
        """返回 0（池内最常出现）到 1（池内只出现一次）的稀有度。"""
        count = counter.get(key, 0)
        if not count:
            return 1.0
        table = self._rank_table(counter_id, counter)
        return table.get(count, 0.5)

    def build(self, names: list[str], tokens_left: list[str], tokens_right: list[str]) -> None:
        self.total = max(1, len(names))
        for name in names:
            self.char.update(name)
            for i in range(3):
                self.bigram[name[i : i + 2]] += 1
        for left, right in zip(tokens_left, tokens_right):
            self.token[left] += 1
            self.token[right] += 1
            self.token_pair[left + "|" + right] += 1



# ---------------------------------------------------------------------------
# 五个维度
# ---------------------------------------------------------------------------

def score_brand(name: str, left: str, right: str, left_category: str, right_category: str, stats: CorpusStats) -> tuple[float, list[str]]:
    base = {
        ("actions", "images"): 8.1,
        ("actions", "abstracts"): 8.5,
        ("images", "actions"): 7.4,
        ("abstracts", "actions"): 7.0,
        ("images", "abstracts"): 7.1,
        ("abstracts", "images"): 6.7,
    }.get((left_category, right_category), 7.0)

    score = base
    notes: list[str] = []

    head_motion = sum(1 for char in left if char in STRONG_MOTION)
    if left_category == "actions" and head_motion >= 1:
        score += 0.7
        notes.append("首个词元自带动作，品牌动势直接。")
    elif left_category == "actions" and all(char in WEAK_MOTION for char in left):
        score -= 0.3

    if right in OPEN_ENDINGS:
        score += 0.6
        notes.append("收尾留有余地，便于延展到产品和口号。")

    if name.startswith("有") and len(name) == 4 and name[1] in "序信度方光为道恒":
        score -= 0.5

    # 词元组合越少见，越像一个新造的品牌词
    pair_rarity = stats.rarity(stats.token_pair, left + "|" + right, 1)
    score += (pair_rarity - 0.5) * 0.8

    if cliche_hit(name):
        score -= 1.2
        notes.append("含常见行业词，容易被归入同行堆里。")

    return score, notes


def score_readability(name: str, profile: dict, common_chars: set[str]) -> tuple[float, list[str]]:
    score = 6.4
    notes: list[str] = []

    common_count = sum(1 for char in name if char in common_chars)
    score += {4: 1.5, 3: 1.0, 2: 0.3}.get(common_count, -0.6)

    tone_score, tone_notes = tone_curve_score(profile["tones"])
    score += tone_score - 7.0
    notes.extend(tone_notes)

    phonetic_score, phonetic_notes = phonetic_repetition_score(profile)
    score += phonetic_score
    notes.extend(phonetic_notes)

    if len(set(name)) < 4:
        score -= 1.2
        notes.append("四字内有重复字，读起来像叠词拼凑。")

    return score, notes


def score_poetry(name: str, left_category: str, right_category: str, stats: CorpusStats) -> tuple[float, list[str]]:
    score = 5.6
    notes: list[str] = []

    image_count = int(left_category == "images") + int(right_category == "images")
    score += {2: 1.9, 1: 0.9, 0: 0.0}[image_count]

    sensory = sum(1 for char in name if char in SENSORY_CHARS)
    score += min(2.0, sensory * 0.5)
    if sensory >= 3:
        notes.append("意象密度高，画面感直接。")

    if any(char in SEASON_CHARS for char in name):
        score += 0.5
        notes.append("带季节或时辰，名字里有时间流动。")

    # 全部是抽象字时，画面感最弱
    if left_category == "abstracts" and right_category == "abstracts":
        score -= 0.8
        notes.append("四字都由抽象词构成，缺少具体画面。")

    if idiom_hit(name):
        score -= 2.4
        notes.append("与常见成语完全重合，诗意被现成表达吃掉。")

    if name in INCOHERENT_BIGRAMS:
        score -= 1.0

    char_rarity = sum(stats.rarity(stats.char, char, 2) for char in name) / 4
    score += (char_rarity - 0.5) * 1.0

    return score, notes


def score_distinctiveness(name: str, left: str, right: str, stats: CorpusStats) -> tuple[float, list[str]]:
    notes: list[str] = []
    score = 5.4

    bigram_rarity = sum(
        stats.rarity(stats.bigram, name[i : i + 2], 3) for i in range(3)
    ) / 3
    score += bigram_rarity * 3.4

    if bigram_rarity > 0.88:
        notes.append("字组搭配少见，辨识度高。")
    elif bigram_rarity < 0.35:
        notes.append("字组搭配较常见，记忆点偏弱。")

    char_rarity = sum(stats.rarity(stats.char, char, 2) for char in name) / 4
    score += char_rarity * 1.2

    # 注意：不能用"词元在候选池里出现几次"来扣分。
    # 生成方式是每个词元配全部对家，所有词元都会固定产出约 155 条，
    # 那个频次对所有人几乎相同，扣分等于给全体统一减分，笔记也会人人命中。
    # 这里改用字表里的真实字频：越是由最高频汉字拼成，越容易被听成"常见词"。
    common_ratio = sum(1 for char in name if stats.char_rank.get(char, 9999) <= 800) / 4
    score -= common_ratio * 0.9
    if common_ratio == 1.0:
        notes.append("四字都是最高频汉字，容易被当成通用词而非字号。")

    return score, notes


def score_registration(name: str, left: str, right: str, stats: CorpusStats) -> tuple[float, list[str]]:
    """注册启发分：估计撞名与商标冲突的面有多大。

    这个分数无法替代真实检索，但"用字有多常见"是它最诚实的代理指标：
    全由高频常用字组成、又是常见结构的四字名，几乎一定已经有大量同名主体。
    旧版这里把词对频次恒等于 1 当加分项，结果三万多条里 99% 都拿同一个分，
    等于没有评分。
    """
    notes: list[str] = []
    score = 6.2

    ranks = [stats.char_rank.get(char, 4000) for char in name]
    average_rank = sum(ranks) / len(ranks)
    # 用 log 把 1..8105 的序号摊开，再映射到 -1.9..+1.9。
    # 阶梯函数会让大量候选落在同一档，连续映射才能体现"稍微更冷僻一点"的差别。
    normalized = math.log1p(min(average_rank, 8000)) / math.log1p(8000)
    score += (normalized - 0.62) * 4.6

    if average_rank <= 300:
        notes.append("四字均由最高频汉字构成，同名主体大概率已经很多。")
    elif average_rank >= 2200:
        notes.append("用字避开最高频区间，字号撞车的概率明显更低。")

    if sum(1 for rank in ranks if rank <= 500) == 4:
        score -= 0.6

    if idiom_hit(name):
        score -= 3.0
        notes.append("整词命中已知成语，注册冲突面明显更大。")

    if cliche_hit(name):
        score -= 1.4
        notes.append("含常见行业词，与其他主体重名的可能性更高。")

    if stats.token.get(left, 0) >= 300 and stats.token.get(right, 0) >= 300:
        score -= 0.6
        notes.append("两侧词元都极常见，组合空间被大量占用。")

    return score, notes


# ---------------------------------------------------------------------------
# 总装
# ---------------------------------------------------------------------------

REASON_TEMPLATES = {
    "brand": (
        "动作或方向感明显，像一个正在发生的品牌。",
        "有清晰的品牌叙事，后续可延展到口号和产品线。",
        "品牌叙事偏弱，更像词组而非字号。",
    ),
    "readability": (
        "四字节奏自然，常用字占比高，口头传播负担低。",
        "整体顺口，个别字需要在真实传播中观察。",
        "读音有拗口或含混处，口头传播会打折。",
    ),
    "poetry": (
        "有明确画面，但没有直接落入常见成语。",
        "保留古典气质，同时有一定现代转译空间。",
        "意象单薄，更接近功能性词组。",
    ),
    "distinctiveness": (
        "字组搭配少见，识别度和记忆点都较好。",
        "有一定辨识度，但需结合行业进一步筛选。",
        "字组过于常见，同质化风险高。",
    ),
    "registration": (
        "未落入明显的成语结构，启发式冲突风险较低。",
        "暂未见明显高风险结构，但仍需做真实检索。",
        "命中常见表达或高频结构，需优先做真实检索。",
    ),
}


def reason_text(key: str, value: float) -> str:
    good, mid, low = REASON_TEMPLATES[key]
    text = good if value >= 8.0 else mid if value >= 6.5 else low
    return f"{DIMENSION_LABELS[key]} {value:.1f}/10：{text}"


@dataclass
class NameScore:
    scores: dict[str, float]
    total: float
    reasons: list[str]
    notes: list[str]


def score_name(
    name: str,
    left: str,
    right: str,
    left_category: str,
    right_category: str,
    common_chars: set[str],
    stats: CorpusStats,
    profile: dict | None = None,
) -> NameScore:
    profile = profile or pinyin_profile(name)

    raw: dict[str, tuple[float, list[str]]] = {
        "brand": score_brand(name, left, right, left_category, right_category, stats),
        "readability": score_readability(name, profile, common_chars),
        "poetry": score_poetry(name, left_category, right_category, stats),
        "distinctiveness": score_distinctiveness(name, left, right, stats),
        "registration": score_registration(name, left, right, stats),
    }

    scores: dict[str, float] = {}
    notes: list[str] = []
    for key, (value, extra) in raw.items():
        scores[key] = round(min(10.0, max(0.0, value)), 1)
        notes.extend(extra)

    total = round(
        sum(scores[key] * WEIGHTS[key] for key in WEIGHTS) / 10,
        1,
    )

    reasons = sorted(
        (reason_text(key, scores[key]) for key in scores),
        key=lambda line: -float(line.split(" ")[1].split("/")[0]),
    )[:3]

    return NameScore(scores=scores, total=total, reasons=reasons, notes=notes)
