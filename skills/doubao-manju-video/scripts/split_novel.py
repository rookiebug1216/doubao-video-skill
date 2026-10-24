#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
分镜拆解引擎：把小说章节 / 故事描述拆分为 <=15 秒的漫剧镜头。

核心约束：豆包免费版单次生成最长 15 秒，因此每个镜头的时长必须 <= 15s。

拆解依据（双因子）：
  1. 字数密度  —— 按中文阅读语速（约 5.2 字/秒叙述节奏）估算镜头容量
  2. 内容结构 —— 按叙事节拍（动作/转折/情绪/场景）切分，优先在语义边界断开

分集与悬念结尾（第三因子）：
  镜头的"自然节奏"不动，但每集最多 --episode-shots 镜（默认 5 镜 ≈ 60 秒，
  对齐主流微短剧单集 1-2 分钟的下沿，也正好一天免费额度能跑完一集）。
  连载中的章节，每集结尾必须停在"精彩部分过半之后"的悬念处；
  若该集尾部是收束性内容（低张力镜头，或含"终于/从此/恢复平静"这类归位词），
  就把这些镜头顺延到下一集开头——即"在高潮未解决处切断"。

用法：
  python split_novel.py chapter.txt -o out/                 # 从小说文件拆解
  python split_novel.py chapter.txt -o out/ --style 3d-cartoon --ratio 9:16   # 竖屏短视频（默认 16:9 横屏）
  python split_novel.py chapter.txt -o out/ --episode-shots 5   # 每集最多 5 镜
  python split_novel.py chapter.txt -o out/ --is-final          # 完结章：末集正常收尾
  python split_novel.py --from-plan plan.json -o out/       # 从已有故事梗概拆解
  python split_novel.py chapter.txt -o out/ --json-only      # 只输出 JSON 供程序消费
"""

import argparse
import json
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field, asdict

# ── 加载共享内核：画风预设 ──
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
from shared import style_presets
from shared.project_manager import ProjectManager, resolve_output_dir

# ──────────────────────────────────────────────────────────────
# 常量
# ──────────────────────────────────────────────────────────────

MAX_SHOT_SECONDS = 15          # 豆包免费版单视频硬上限
MIN_SHOT_SECONDS = 4           # 低于 4 秒的镜头没有叙事价值，宁可与相邻合并

# 每集镜头数上限：主流微短剧单集 1-2 分钟，自然节奏下 5 镜 ≈ 60 秒，
# 落在下沿；同时约等于豆包免费额度（约 5 次/天），即"一天一集"
EPISODE_SHOTS = 5
# 一集至少保留几镜：低于此数就不成集，宁可让悬念镜头留着结尾
MIN_EPISODE_SHOTS = 2

# 节拍张力：>= HOOK_TENSION 的镜头才有资格收尾一集（精彩部分）
BEAT_TENSION = {
    "fight":   3.0,
    "action":  3.0,
    "turn":    3.0,
    "emotion": 2.5,
    "dialog":  2.0,
    "narration": 1.0,
    "atmos":   0.5,
}
HOOK_TENSION = 2.0
# 归位/收束词：命中即视为"剧情已落地"，不适合做悬念结尾
RESOLVE_RE = re.compile(
    r"终于|从此|就这样|再也没|再也没有|永远|结束了|落幕|尘埃落定|"
    r"归于平静|恢复平静|平静下来|长舒一口气|睡去|安然|安稳|失去了知觉"
)


# 中文叙述节奏基准：字/秒。漫剧旁白语速略慢于默读。
CHARS_PER_SEC = 4.6
# 每个镜头除旁白外，留给纯画面演绎的"呼吸"时间（秒）
VISUAL_PADDING = 2.0
# 场景切换类镜头需要更长的建立时间
SCENE_ESTABLISH_PADDING = 3.5

# 画风预设与画幅映射 —— 从 shared/style_presets.py 引入，保证全 skill 链一致
STYLE_PRESETS = style_presets.STYLE_PRESETS
RATIO_MAP = style_presets.RATIO_MAP

# 叙事节拍权重：不同节拍在视觉上的停留时间不同
BEAT_PATTERNS = [
    ("action", re.compile(r"突然|猛地|一下|挥|冲|扑|砍|刺|爆|轰|砸|撞|奔|逃|攻击|闪身|跃|抬腿|出手")),
    ("turn",   re.compile(r"但是|然而|却|不料|竟然|没想到|原来|忽然明白|真相|反转|其实|直到")),
    ("emotion",re.compile(r"眼眶|泪|颤抖|苦笑|心跳|恐惧|愤怒|震惊|心疼|温柔|释然|怔")),
    ("dialog", re.compile(r"[“\"「].{2,60}[”\"」]|说道|问道|喊道|低声|喃喃")),
    ("atmos",  re.compile(r"夜|清晨|黄昏|雨|雪|风|雾|光|寂静|空旷|远方|天空|灯火")),
]

# ── 打斗场景检测（fight 节拍，优先于 action）──
# 设计原则：只有"正在发生肢体/兵器交锋"的文本才判为打斗；
# 静态持械（握紧长剑）、方位移动（追来、跑开）不算，避免把普通动作镜误判成打斗。
# 显性打斗词：单词命中即判打斗
FIGHT_EXPLICIT_RE = re.compile(
    r"打斗|交手|过招|搏斗|厮杀|激战|对决|决战|决斗|缠斗|混战|围攻|迎战|应战|"
    r"格挡|招架|肉搏|火拼|开战|枪战|比武|切磋|大打出手|打架|厮打|斗殴|群殴|动武"
)
# 械斗组合：兵器/拳脚 与 攻击动作 同现才算打斗（排除"握紧长剑"这类静态持械）
# 注意：不收"腿"（"拔腿就跑"会误判），扫堂腿类由"拳/掌"或显性词兜底
FIGHT_WEAPON_RE = re.compile(
    r"长剑|短剑|佩剑|宝剑|利剑|软剑|剑|大刀|长刀|弯刀|单刀|刀|长枪|红缨枪|"
    r"棍|鞭|匕首|匕|斧|锤|锏|戟|弓|弩|暗器|飞刀|兵器|利刃|拳头|拳|掌|爪"
)
FIGHT_ACT_RE = re.compile(
    r"挥|拔|砍|劈|刺|斩|削|挑|撩|扫|击|打|踢|踹|挡|架|拍|搏|攻|杀|射|轰|砸|撞|闪|交"
)
# 现代枪战/爆炸：无需兵器词，单独判定
FIGHT_GUN_RE = re.compile(r"开了一?枪|开枪|扫射|射击|枪响|爆炸|轰击|炮轰")
# 双方兵器碰撞词（用于判定"火花"是否合理）
FIGHT_CLASH_RE = re.compile(
    r"格挡|招架|架住|碰撞|相撞|交锋|对砍|对劈|互砍|互劈|相交|硬碰硬"
)
# 攻击动作词（因果桥：攻击拍后紧跟结果拍时注入"击中"）
FIGHT_ATTACK_RE = re.compile(r"挥|砍|劈|刺|斩|扫|挑|撩|击|打|踢|踹|砸|撞")
# 受击结果词（倒地/击飞等，需要前置因）
FIGHT_RESULT_RE = re.compile(
    r"倒地|倒下|击飞|打飞|踢飞|踹飞|跌落|坠|踉跄|吐血|重创|毙命"
)


# ── 打斗编排增强模块（因果链：出招 → 命中 → 受击倒地）──
# 用户反馈（2026-10-20）：原提示词只有"挥剑→倒地"，中间没有可见的"命中"环节，
# 生成视频里黑衣人"突然倒地"、因果断裂。本模块让每个受击结果都有前置因，
# 且攻击拍直接点名命中部位，使模型有足够信息渲染"出招→命中→倒地"的连续动作。
#
# 兵器词的"着力点"表达：让"命中"描述具体可感（剑锋/枪尖/拳头…）
_FIGHT_POINT_WORD = {
    "长剑": "剑锋", "短剑": "剑锋", "佩剑": "剑锋", "宝剑": "剑锋",
    "利剑": "剑锋", "软剑": "剑锋", "剑": "剑锋",
    "大刀": "刀锋", "长刀": "刀锋", "弯刀": "刀锋", "单刀": "刀锋", "刀": "刀锋",
    "长枪": "枪尖", "红缨枪": "枪尖", "枪": "枪尖",
    "棍": "棍头", "鞭": "鞭梢", "匕首": "匕首锋", "匕": "匕首锋",
    "斧": "斧刃", "锤": "锤头", "戟": "戟尖", "弓": "箭矢", "弩": "弩箭",
    "拳": "拳头", "掌": "手掌", "爪": "利爪",
}
# 兵器优先级（长兵器/具名优先，避免"长剑"被短词"剑"抢先命中）
_FIGHT_WEAPON_ORDER = (
    "长剑", "短剑", "佩剑", "宝剑", "利剑", "软剑", "剑",
    "红缨枪", "长枪", "枪", "大刀", "长刀", "弯刀", "单刀", "刀",
    "匕首", "匕", "棍", "鞭", "斧", "锤", "戟", "弓", "弩",
    "拳", "掌", "爪",
)
# 命中部位（按拍序轮换，让"命中其X"具体而不重复）
_STRIKE_REGIONS = ("肩头", "胸口", "腰肋", "膝弯")


def _fight_weapon_word(narration: str) -> str:
    """从文本提取兵器词（长兵器/具名优先），无则返回通用「兵器」。"""
    for w in _FIGHT_WEAPON_ORDER:
        if w and w in narration:
            return w
    return "兵器"


def _fight_point_word(weapon: str) -> str:
    """兵器词的着力点表达（剑锋/枪尖/拳头…），无映射则原样返回。"""
    return _FIGHT_POINT_WORD.get(weapon, weapon)


def is_fight_scene(text: str) -> bool:
    """判定一段文本是否描写正在进行的打斗/交锋场景。

    三条独立通路，任一命中即为打斗：
      1. 显性打斗词（打斗/交手/过招/格挡…）
      2. 枪战爆炸词（开枪/扫射/爆炸…）
      3. 兵器拳脚词 + 攻击动作词 同现（挥剑/拔刀/劈砍/拳打脚踢…）
    静态持械（握紧长剑）、单纯移动（追来/跑开）不会命中通路 3。
    """
    if not text:
        return False
    if FIGHT_EXPLICIT_RE.search(text):
        return True
    if FIGHT_GUN_RE.search(text):
        return True
    return bool(FIGHT_WEAPON_RE.search(text) and FIGHT_ACT_RE.search(text))

DEFAULT_MODEL = style_presets.DEFAULT_MODEL


# ──────────────────────────────────────────────────────────────
# 数据结构
# ──────────────────────────────────────────────────────────────

@dataclass
class Shot:
    index: int                    # 镜号，从 1 开始
    seconds: int                  # 时长（秒），<= 15
    beat: str                     # 叙事节拍
    beat_label: str                # 节拍中文名
    scene: str                # 场景描述
    action: str                   # 画面动作
    camera: str                   # 运镜
    narration: str                # 旁白/台词
    source_text: str              # 对应原文片段
    style: str = ""
    ratio: str = ""
    prompt: str = ""              # 视频正文提示词（投豆包，禁角色名，用通用身份）
    frame_prompt: str = ""       # 分镜图提示词（关键帧图，直呼角色名指向设定图）
    end_frame_prompt: str = ""   # 尾帧图提示词（v3：本镜结束定格画面，图生图生成，直呼角色名）
    episode: int = 0              # 所属集号（从 1 开始）
    episode_end: bool = False     # 是否为本集最后一镜
    hook_end: bool = False        # 本集是否以悬念收尾（下一集存在）
    carry_over: bool = False      # 是否为上一集顺延下来的"下集开场"承接镜头
    flat_end: bool = False        # 本应留悬念但未能切出（结尾平缓，需人工处理）
    characters: list = field(default_factory=list)  # 出场角色名列表（用于匹配参考图）
    props: list = field(default_factory=list)        # 出场道具名列表（用于匹配道具参考图，无道具则为空）
    # ── 衔接字段（参考 shuohao-skills blocking 机制）──
    blocking: str = ""            # 站位/朝向描述（人物站位、间距、与镜头关系）
    entry_state: str = ""         # 进入本镜时画面已有的状态（承接上一镜收尾）
    exit_state: str = ""          # 本镜结束时画面定格状态（供下一镜 entry_state 引用）
    # ── 构图量化六字段（参考 shuohao-skills storyboard 六字段）──
    focal_length: str = ""        # 焦距：35mm / 85mm / 广角 / 长焦
    camera_position: str = ""    # 机位：平视 / 俯视 / 仰视 / 低位 / 高位
    composition: str = ""        # 构图：三分法 / 居中 / 对称 / 引导线
    gaze_target: str = ""        # 视线落点：望向画外 / 对视 / 侧望 / 低头
    focus_point: str = ""         # 焦点：人物面部 / 道具 / 远景 / 虚化背景
    stability: str = ""           # 稳定性：稳定三脚架 / 手持轻微晃动 / 跟随运动 / 摇镜
    on_screen_count: int = 0     # 同框人物数（≤3 软约束）

    @property
    def total_cost(self) -> int:
        """按豆包计费规则估算额度：免费版按生成次数计费，与视频时长无关，每镜 1 次。"""
        return 1


@dataclass
class ShotList:
    title: str
    style_key: str
    ratio: str
    style_desc: str
    ratio_desc: str
    model: str
    shots: list = field(default_factory=list)
    source_type: str = "novel"     # novel | concept
    total_chars: int = 0
    episode_shots: int = EPISODE_SHOTS   # 每集镜头数上限（0=不分集）
    is_final: bool = False               # 本章是否为全书完结章
    warnings: list = field(default_factory=list)

    def to_dict(self):
        eps = episode_summary(self.shots)
        return {
            "title": self.title,
            "style_key": self.style_key,
            "ratio": self.ratio,
            "style_desc": self.style_desc,
            "ratio_desc": self.ratio_desc,
            "model": self.model,
            "source_type": self.source_type,
            "total_chars": self.total_chars,
            "max_seconds_per_shot": MAX_SHOT_SECONDS,
            "shot_count": len(self.shots),
            "total_seconds": sum(s.seconds for s in self.shots),
            "estimated_credits": sum(s.total_cost for s in self.shots),
            "episode_shots": self.episode_shots,
            "is_final": self.is_final,
            "episode_count": len(eps),
            "episodes": eps,
            "shots": [asdict(s) for s in self.shots],
            "warnings": self.warnings,
        }


# ──────────────────────────────────────────────────────────────
# 文本解析
# ──────────────────────────────────────────────────────────────

CJK = re.compile(r"[\u4e00-\u9fff]")
SENT_SPLIT = re.compile(r"(?<=[。！？!?…；;])")
DASH_SPLIT = re.compile(r"(?<=[—–-])")

# 章节标题行：这些行是元信息，不是可拍摄的正文，必须排除出分镜
TITLE_LINE = re.compile(
    r"^\s*(?:#{1,6}\s*|第[一二三四五六七八九十百千零\d]+[章节回卷篇部]|"
    r"序[章言]|楔子|尾声|终章|后记|番外)"
)


def is_title_line(line: str) -> bool:
    """判断一行是否为章节标题（不参与分镜）。"""
    s = line.strip()
    if not s:
        return False
    if TITLE_LINE.match(s):
        return True
    # markdown 标题行一律视为标题
    if re.match(r"^#{1,6}\s", s):
        return True
    # 很短且不含句读、长度像标题
    if len(s) <= 20 and not re.search(r"[，。！？；：,.!?;:]", s):
        return True
    return False


def count_cjk(text: str) -> int:
    """统计中文字符数——决定镜头容量的关键指标。"""
    return len(CJK.findall(text))


def split_sentences(text: str) -> list:
    """
    把文本切成句子，保留标点。

    两项关键处理：
    1. 标题行排除——它们是元信息，不是可拍摄内容。
    2. 对白保护——在引号内部不断句，避免"他说："你终于回来了。"被拆成两半。
    """
    text = re.sub(r"\r\n?", "\n", text).strip()
    if not text:
        return []
    parts = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        if is_title_line(line):
            continue
        parts.extend(p for p in _split_quote_aware(line) if p.strip())
    return parts


# 引号配对符
QUOTE_PAIRS = [("“", "”"), ("「", "」"), ("‘", "’")]


def _split_quote_aware(line: str) -> list:
    """
    引号感知断句：在引号闭合前不切分，避免对白被拦腰截断。

    做法：先用 _quote_mask 标记出所有"处于引号内部"的字符位置，
    只在 mask 为 False 的位置做终止标点断句。
    """
    mask = _quote_mask(line)
    out, buf = [], []
    i, n = 0, len(line)

    while i < n:
        ch = line[i]
        buf.append(ch)

        # 只有不在引号内部的终止标点才允许断句
        if not mask[i] and ch in "。！？!?…；;":
            out.append("".join(buf))
            buf = []

        i += 1

    if buf:
        out.append("".join(buf))
    return out


def _quote_mask(line: str) -> list:
    """标记每个字符是否处于引号内部（True=在引号内）。"""
    mask = [False] * len(line)
    for op, cl in QUOTE_PAIRS:
        depth = 0
        for idx, ch in enumerate(line):
            if ch == op:
                mask[idx] = True
                depth += 1
            elif ch == cl and depth > 0:
                mask[idx] = True
                depth -= 1
            elif depth > 0:
                mask[idx] = True
    # 直引号 " 成对处理
    straight_open = None
    for idx, ch in enumerate(line):
        if ch == '"':
            if straight_open is None:
                straight_open = idx
            else:
                for k in range(straight_open, idx + 1):
                    mask[k] = True
                straight_open = None
    return mask


def strip_markup(s: str) -> str:
    """去掉 markdown 记号，得到纯净小说正文。"""
    s = re.sub(r"^#{1,6}\s*", "", s)
    s = re.sub(r"^\s*[-*+]\s+", "", s)
    s = re.sub(r"^\s*>\s?", "", s)
    return s.strip()


def detect_beat(sentence: str) -> str:
    """判定句子的叙事节拍——决定该给多少视觉停留时间。

    打斗优先于动作：正在交锋的文本判 fight（驱动打斗专用运镜/构图/时间线），
    普通动作（回头/奔跑/抬手）仍归 action。
    """
    if is_fight_scene(sentence):
        return "fight"
    for name, pattern in BEAT_PATTERNS:
        if pattern.search(sentence):
            return name
    return "narration"


BEAT_LABEL = {
    "fight":   "打斗",
    "action":  "动作",
    "turn":    "转折",
    "emotion": "情绪",
    "dialog":  "对白",
    "atmos":   "氛围",
    "narration": "叙述",
}
BEAT_PAD = {
    "fight":   2.0,   # 打斗交锋需要让观众看清招式往来
    "action":  1.5,   # 动作需要额外时间让观众看清
    "turn":    2.0,   # 转折需要停顿回味
    "emotion": 3.0,   # 情绪需要停留
    "dialog":  2.5,   # 对白需要说完整
    "atmos":   SCENE_ESTABLISH_PADDING,
    "narration": 0.0,
}


# ──────────────────────────────────────────────────────────────
# 核心拆解算法
# ──────────────────────────────────────────────────────────────

def estimate_seconds(text: str, beat: str) -> float:
    """估算一段文本作为镜头时的时长。"""
    n = count_cjk(text)
    speak = n / CHARS_PER_SEC              # 旁白朗读耗时
    return speak + VISUAL_PADDING + BEAT_PAD.get(beat, 0.0)


def greedy_group(sentences: list) -> list:
    """
    贪心装箱：把句子装进时长 <=15s 的镜头。

    这是双因子拆解的"字数密度"侧——累积字数直到触及 15s 上限。
    """
    groups, cur, cur_txt, cur_sec = [], [], "", 0.0

    for sent in sentences:
        clean = strip_markup(sent)
        if not clean:
            continue
        beat = detect_beat(clean)
        sec = estimate_seconds(clean, beat)

        # 单句就超 15s：按逗号再切一刀
        if sec > MAX_SHOT_SECONDS:
            if cur:
                groups.append((cur_txt, cur_sec))
                cur, cur_txt, cur_sec = [], "", 0.0
            groups.extend(_split_long_sentence(clean))
            continue

        # 装得下就装，装不下就开新镜头
        if cur_sec + sec <= MAX_SHOT_SECONDS:
            cur.append(sent)
            cur_txt += clean
            cur_sec += sec
        else:
            if cur:
                groups.append((cur_txt, cur_sec))
            cur, cur_txt, cur_sec = [sent], clean, sec

    if cur:
        groups.append((cur_txt, cur_sec))

    # 合并过短镜头，并规整为 (str, float) 元组列表
    merged = _merge_short(groups)

    # 最终校验：任何仍超 15s 的块强制按字数重切
    final = []
    for txt, sec in merged:
        if sec > MAX_SHOT_SECONDS:
            final.extend(_split_by_length(txt))
        else:
            final.append((txt, sec))
    return final


def _split_long_sentence(sentence: str) -> list:
    """
    把超长单句切成多块，每块 <=15s。

    三级降级策略，保证任何输入都能被切开：
      1. 按逗号切（保留语义边界）
      2. 按字数硬切（文本无标点时兜底）
      3. 仍超限则继续按字数递归
    """
    chunks = re.split(r"(?<=[，,、；;：:])", sentence)
    out, cur, cur_sec = [], "", 0.0

    for ch in chunks:
        if not ch:
            continue
        sec = estimate_seconds(ch, detect_beat(ch))

        # 单个逗号块仍超限：按字数硬切
        if sec > MAX_SHOT_SECONDS:
            if cur:
                out.append((cur, cur_sec))
                cur, cur_sec = "", 0.0
            out.extend(_split_by_length(ch))
            continue

        if cur_sec + sec > MAX_SHOT_SECONDS and cur:
            out.append((cur, cur_sec))
            cur, cur_sec = ch, sec
        else:
            cur += ch
            cur_sec += sec

    if cur:
        out.append((cur, cur_sec))
    return out


def _split_by_length(text: str) -> list:
    """
    纯按字数硬切，保证每块时长 <=15s。

    用于完全无标点、或逗号块仍过长的退化场景。

    上限推导：按最大节拍补偿预留余量
        max_chars = (15s - 最大节拍补偿 - 视觉呼吸) × 每秒字数
        最大节拍补偿 = 3.5（氛围镜头），视觉呼吸 = 2.0
        => (15 - 3.5 - 2.0) × 4.6 ≈ 43 字
    该上限保证任何节拍下都不会超过 15s，无需递归或二次修正。
    """
    text = text.strip()
    if not text:
        return []

    total = count_cjk(text)
    if total == 0:
        return [(text, estimate_seconds(text, "narration"))]

    # 保守上限：留足最大节拍补偿空间
    max_pad = max(BEAT_PAD.values())
    max_chars = int((MAX_SHOT_SECONDS - VISUAL_PADDING - max_pad) * CHARS_PER_SEC)
    max_chars = max(20, max_chars)

    # 向上取整分块，确保每块字数 <= max_chars
    n_chunks = max(1, -(-total // max_chars))
    per = -(-total // n_chunks)

    out, cur, cur_n = [], "", 0
    for ch in text:
        cur += ch
        if CJK.match(ch):
            cur_n += 1
        if cur_n >= per:
            out.append((cur, estimate_seconds(cur, detect_beat(cur))))
            cur, cur_n = "", 0
    if cur.strip():
        out.append((cur, estimate_seconds(cur, detect_beat(cur))))

    # 理论不会超限，此处仅作防御性钳制（不递归，避免极端输入下的栈溢出）
    return [(t, min(s, MAX_SHOT_SECONDS)) for t, s in out]


def _merge_short(groups: list) -> list:
    """把 <MIN_SHOT_SECONDS 的镜头并入相邻镜头。"""
    if not groups:
        return []
    merged = [list(groups[0])]
    for txt, sec in groups[1:]:
        if merged[-1][1] < MIN_SHOT_SECONDS and merged[-1][1] + sec <= MAX_SHOT_SECONDS:
            merged[-1][0] += txt
            merged[-1][1] += sec
        else:
            merged.append([txt, sec])
    # 最后一片若过短，并回上一片（若不超限）
    if len(merged) > 1 and merged[-1][1] < MIN_SHOT_SECONDS:
        if merged[-2][1] + merged[-1][1] <= MAX_SHOT_SECONDS:
            merged[-2][0] += merged[-1][0]
            merged[-2][1] += merged[-1][1]
            merged.pop()
    return [tuple(g) for g in merged]


# ──────────────────────────────────────────────────────────────
# 镜头要素生成
# ──────────────────────────────────────────────────────────────

CAMERA_BY_BEAT = {
    # 打斗：多条逗号分隔的运镜短语，build_video_prompt 按时间线节拍轮换，
    # 避免全程单一"动态跟拍"导致的平铺感
    "fight":   "打击点快速推近特写，环绕运镜跟拍交锋，低角度仰拍挥击瞬间，手持晃动紧跟闪避动作，兵器碰撞瞬间大特写",
    "action":  "跟随镜头快速推进，动态跟拍，轻微手持晃动增强临场感",
    "turn":    "缓慢推近至面部特写，浅景深虚化背景",
    "emotion": "面部特写缓慢环绕，柔和光影，眼神光清晰",
    "dialog":  "过肩镜头正反打，平稳固定机位",
    "atmos":   "大远景缓慢横移摇镜，展现环境全貌",
    "narration":"中景缓慢平移，平稳稳定构图",
}


# 构图量化六字段按节拍映射（参考 shuohao-skills storyboard 每镜六字段）
# 与 CAMERA_BY_BEAT 配套使用，build_shot 阶段取默认值
COMPOSITION_BY_BEAT = {
    "fight":    {"focal_length": "广角16-24mm", "camera_position": "低位仰视",
                 "composition": "对角线动感", "gaze_target": "对视或侧望",
                 "focus_point": "动作主体", "stability": "手持晃动跟随运动"},
    "action":   {"focal_length": "广角16-24mm", "camera_position": "低位仰视",
                 "composition": "对角线动感", "gaze_target": "对视或侧望",
                 "focus_point": "动作主体", "stability": "手持轻微晃动"},
    "turn":     {"focal_length": "85mm中长焦", "camera_position": "平视特写",
                 "composition": "居中", "gaze_target": "对视",
                 "focus_point": "人物面部", "stability": "稳定三脚架"},
    "emotion":  {"focal_length": "85mm中长焦", "camera_position": "平视面部特写",
                 "composition": "三分法", "gaze_target": "低头或侧望",
                 "focus_point": "人物面部", "stability": "缓慢环绕"},
    "dialog":   {"focal_length": "50mm标准", "camera_position": "过肩平视",
                 "composition": "正反打", "gaze_target": "对视",
                 "focus_point": "说话人", "stability": "稳定固定机位"},
    "atmos":    {"focal_length": "广角24-35mm", "camera_position": "高位俯视",
                 "composition": "三分法", "gaze_target": "望向远方",
                 "focus_point": "远景环境", "stability": "大远景横移摇镜"},
    "narration":{"focal_length": "35mm标准", "camera_position": "平视中景",
                 "composition": "三分法", "gaze_target": "侧望",
                 "focus_point": "人物主体", "stability": "缓慢平移"},
}


def extract_scene(text: str, prev_scene: str = "") -> str:
    """
    从文本推断场景描述。

    三级策略：
    1. 匹配具体地点名词（青石巷/厨房/教室）
    2. 叠加时间氛围（深夜/雨中）
    3. 沿用上一镜头场景（同一场景的连续镜头不该每镜都换景）
    """
    # 地点后缀优先——具体地名比泛化词更能指导画面
    place = re.findall(
        # 通用地点后缀：前取1-3字，优先匹配双字词（如"教室""厨房"）
        r"[\u4e00-\u9fff]{0,3}(?:教室|厨房|卧室|书房|客厅|阳台|屋顶|车厢|客栈|酒楼|茶馆|书院|广场|走廊|院子|阳台)"
        r"|[\u4e00-\u9fff]{1,3}(?:门口|门前|门外|窗前|桌上|地上|空中|水上|掌中|巷口|山巅|崖边)"
        r"|[\u4e00-\u9fff]{1,3}(?:城中村|街道|巷子|山峰|树林|洞口|海面)"
        r"|(?:青石|石|泥|土|木|砖)[一-龥]{1,2}?(?:板|地|路|桥|墙|屋|房)"
        r"|[\u4e00-\u9fff]{1,2}(?:城|村|镇|山|河|湖|街|巷|路|店|房|屋|室|厅|楼|阁|园|院|庙|寺|塔|桥|铺|馆|殿|峰|林|洞|海|岛|校|坊)",
        text,
    )
    scene = ""
    if place:
        # 固定地点词表优先——命中即采用，不做前后缀拼接
        FIXED_PLACES = ("巷口", "门口", "门前", "门外", "窗前", "桌上", "地上",
                        "空中", "水上", "掌中", "山巅", "崖边", "教室", "厨房",
                        "卧室", "书房", "客厅", "阳台", "屋顶", "车厢", "客栈",
                        "酒楼", "茶馆", "书院", "广场", "走廊", "院子", "街道")
        # 方位词不是独立场景：地上/桌上/窗前等只表示位置，不能覆盖真实地点。
        # 镜3"望着地上的人"若命中"地上"，会错误地把"雨夜的客栈"切到野外。
        POSITIONAL = ("桌上", "地上", "空中", "水上", "掌中", "巷口", "门口",
                      "门前", "门外", "窗前", "山巅", "崖边")
        fixed_hit = [p for p in FIXED_PLACES if p in text]
        # 有上一镜场景时，方位词命中的候选降级（不当新场景，沿用上一镜）
        if prev_scene:
            fixed_hit = [p for p in fixed_hit if p not in POSITIONAL]
        if fixed_hit:
            scene = max(fixed_hit, key=len)
        else:
            # 候选清洗：剔除动词误配（"霜望着地上"里的前缀不是地点）
            bad_prefix = ("想要", "看清", "望", "看", "来", "走近", "走", "转", "抓",
                          "松", "听", "冲", "扑", "抬", "低", "找", "着")
            cand = []
            for p in place:
                if len(p) < 2:
                    continue
                # 以方位词结尾的候选（如"霜望着地上"）：截取方位词本身，再按 prev_scene 决定去留
                pos_hit = next((pos for pos in POSITIONAL if p.endswith(pos) and len(p) > len(pos)), None)
                if pos_hit:
                    if not prev_scene:
                        cand.append(pos_hit)  # 无上一镜时才允许方位词独立成景
                    continue
                if any(p.startswith(b) for b in bad_prefix):
                    continue
                if prev_scene and p in POSITIONAL:
                    continue
                cand.append(p)
            if cand:
                scene = max(cand, key=len)

    # 时间 / 天气氛围叠加
    atmos = [
        w for w in ("深夜", "黎明", "清晨", "正午", "午后", "黄昏", "傍晚", "夜晚",
                    "雪中", "雨中", "雾中", "风中", "雪夜", "雨夜")
        if w in text
    ]
    at = atmos[0] if atmos else ""

    if scene and at:
        return f"{at}的{scene}"
    if scene:
        return scene
    if at:
        return f"{at}的{scene_hint_from_action(text)}" if scene_hint_from_action(text) else f"{at}的街道"
    # 无新地点信息：沿用上一镜头场景，避免每镜都写"故事主场景"
    if prev_scene:
        return prev_scene
    return scene_hint_from_action(text) or "故事主场景"


def scene_hint_from_action(text: str) -> str:
    """从动作描述反推场景线索。"""
    hints = [
        (r"门[开合]|门槛|跨过", "门前"),
        (r"雨|雪|风|雾", "户外"),
        (r"桌|案|椅|床", "室内"),
        (r"街|巷|路|广场", "街道"),
        (r"山|峰|林|谷|河|海", "野外"),
    ]
    for pattern, name in hints:
        if re.search(pattern, text):
            return name
    return ""


def build_shot(index: int, text: str, sec: float, style: str, ratio_desc: str,
               prev_scene: str = "", prev_shot: "Shot" = None,
               cast_map: dict = None, no_continuity: bool = False) -> Shot:
    """把一段文本组装成完整镜头对象。

    双提示词分工（参考 shuohao-skills novel-storyboard）：
      * prompt      → 视频正文，禁角色名，用通用身份（持剑青年/白衣女子）避免姓名污染
      * frame_prompt → 分镜图，直呼角色名指向设定图，描述镜头第一格定格画面
    build_shot 阶段尚未完成角色标注，characters 暂为 []；
    annotate_characters 完成后会调用 _finalize_prompts 用 shot.characters 重新生成。

    衔接+构图升级（参考 shuohao-skills blocking + 六构图字段）：
      * prev_shot.exit_state → 本镜 entry_state（承接上一镜末帧画面状态）
      * _infer_state_from_tail 反推本镜 exit_state（供下一镜引用）
      * _infer_blocking 推断站位/朝向
      * COMPOSITION_BY_BEAT 按节拍取六构图字段默认值
    no_continuity=True 时跳过衔接注入（独立单镜/调试场景）。
    """
    beat = detect_beat(text)
    secs = max(MIN_SHOT_SECONDS, min(MAX_SHOT_SECONDS, int(round(sec))))
    scene = extract_scene(text, prev_scene)

    # 清理原文：合并重复标点，避免提示词出现"。。"
    narration = re.sub(r"\s+", " ", text).strip()
    narration = re.sub(r"([。，！？；：])\1+", r"\1", narration)

    camera = CAMERA_BY_BEAT.get(beat, CAMERA_BY_BEAT["narration"])
    comp = COMPOSITION_BY_BEAT.get(beat, COMPOSITION_BY_BEAT["narration"])

    # ── 衔接字段 ──
    if no_continuity or prev_shot is None:
        entry_state = ""
    elif prev_shot.exit_state:
        entry_state = prev_shot.exit_state
    else:
        # 上一镜未生成 exit_state（兼容旧数据），从其 narration 末句反推
        entry_state = _infer_state_from_tail(prev_shot.narration, prev_shot.scene, is_exit=True)
    exit_state = "" if no_continuity else _infer_state_from_tail(narration, scene, is_exit=True)
    # blocking 在 build_shot 阶段 characters 还未标注，先用原文预判
    blocking = "" if no_continuity else _infer_blocking([], narration, scene, cast_map)

    video_prompt = build_video_prompt(
        scene, narration, [], camera, style, ratio_desc, secs,
        entry_state=entry_state, blocking=blocking,
        focal_length=comp["focal_length"], camera_position=comp["camera_position"],
        composition=comp["composition"], gaze_target=comp["gaze_target"],
        focus_point=comp["focus_point"], stability=comp["stability"],
        cast_map=cast_map,
    )
    frame_prompt = build_frame_prompt(
        scene, narration, [], camera, style, ratio_desc,
        entry_state=entry_state, blocking=blocking,
        focal_length=comp["focal_length"], camera_position=comp["camera_position"],
        composition=comp["composition"], gaze_target=comp["gaze_target"],
        focus_point=comp["focus_point"], stability=comp["stability"],
        cast_map=cast_map,
    )

    return Shot(
        index=index,
        seconds=secs,
        beat=beat,
        beat_label=BEAT_LABEL.get(beat, beat),
        scene=scene,
        action=narration[:60],
        camera=camera,
        narration=narration,
        source_text=narration,
        style=style,
        ratio=ratio_desc,
        prompt=video_prompt,
        frame_prompt=frame_prompt,
        blocking=blocking,
        entry_state=entry_state,
        exit_state=exit_state,
        focal_length=comp["focal_length"],
        camera_position=comp["camera_position"],
        composition=comp["composition"],
        gaze_target=comp["gaze_target"],
        focus_point=comp["focus_point"],
        stability=comp["stability"],
        on_screen_count=0,  # 等 annotate_characters 后由 _finalize_prompts 回填
    )


# ── 通用身份映射：分镜图直呼角色名 vs 视频正文禁角色名 ──

# 单字道具词，不参与角色名替换（避免把"剑"误替换成"持剑青年"）
_PROP_WORDS = {"剑", "刀", "弓", "枪", "棍", "鞭", "锤", "盾", "琴", "笔", "伞", "镜"}
_GENERIC_FALLBACK = ["人物甲", "人物乙", "人物丙", "人物丁"]


# ── 衔接辅助函数（参考 shuohao-skills blocking + 切点对账）──

# 动作动词 → 末帧状态 推断映射
_TAIL_ACTION_PATTERNS = [
    (re.compile(r"挥剑|挥刀|出剑|出鞘|拔剑|拔刀"), "剑已出鞘，寒光定格"),
    (re.compile(r"收剑|收刀|入鞘|归鞘"), "剑已入鞘，手垂身侧"),
    (re.compile(r"冲|扑|奔|跃|跳|追"), "身影疾冲，画面定格在动作中段"),
    (re.compile(r"倒地|倒下|跌坐|栽倒"), "身体倒地，画面定格在落地瞬间"),
    (re.compile(r"转身离去|转身走|转身离开|拂袖而去"), "背影远去，画面定格在背影"),
    (re.compile(r"回头|转身回头看|猛地回头"), "回头瞬间，侧脸面向镜头"),
    (re.compile(r"握紧|握住|抓紧"), "手握紧物体，画面定格在握物特写"),
    (re.compile(r"低头|垂首|俯首"), "低头垂目，画面定格在俯视角度"),
    (re.compile(r"抬头|仰望|抬眼"), "抬头仰望，画面定格在仰视角度"),
    (re.compile(r"闭眼|闭目|合眼"), "双目微阖，画面定格在闭眼瞬间"),
    (re.compile(r"睁眼|睁目|开眼"), "双目睁开，画面定格在睁眼瞬间"),
    (re.compile(r"握拳|攥拳"), "拳头紧握，画面定格在握拳特写"),
    (re.compile(r"撞开|撞入|撞破"), "门被撞开，画面定格在冲击瞬间"),
    (re.compile(r"颤声|惊呼|喊道|嘶吼"), "神情紧张，画面定格在呼喊瞬间"),
]
_TAIL_EMOTION_PATTERNS = [
    (re.compile(r"泪|哭泣|落泪|流泪|眼眶.*?红"), "眼眶含泪，表情凝重"),
    (re.compile(r"苦笑|惨笑|强笑"), "苦笑定格，眼神带悲"),
    (re.compile(r"惊|怔|愣|愕"), "神情震惊，目光定格"),
    (re.compile(r"怒|愤|气恼"), "怒容定格，眉头紧锁"),
    (re.compile(r"笑"), "笑容定格，神情舒展"),
]
_TAIL_ATMOSP_PATTERNS = [
    (re.compile(r"雨"), "雨幕未停，画面定格在雨景"),
    (re.compile(r"雪"), "雪花飘落，画面定格在雪景"),
    (re.compile(r"夜|夜晚|深夜"), "夜色深沉，画面定格在夜景"),
    (re.compile(r"晨|清晨|黎明"), "晨光初现，画面定格在晨景"),
    (re.compile(r"黄昏|夕阳|落日"), "夕阳余晖，画面定格在暮色"),
]


def _infer_state_from_tail(narration: str, scene: str = "", is_exit: bool = False) -> str:
    """
    从 narration 末句反推画面定格状态，用于衔接下一镜。

    策略：取末句（引号感知），按动作/情绪/氛围三类匹配，命中即返回；
    都不命中则返回空字符串（不强行注入衔接信息）。
    输出短句（≤30字），用于下一镜 entry_state。
    """
    if not narration:
        return ""
    # 取末句（引号感知，避免在引号内部切断）
    tail = _last_clause_quote_aware(narration)
    if not tail:
        tail = narration
    # 截取末 50 字以加速匹配
    tail = tail[-50:]

    # 优先级：动作 > 情绪 > 氛围
    for patterns in (_TAIL_ACTION_PATTERNS, _TAIL_EMOTION_PATTERNS, _TAIL_ATMOSP_PATTERNS):
        for pat, state in patterns:
            if pat.search(tail):
                return state
    return ""


def _last_clause_quote_aware(narration: str) -> str:
    """取末句，引号感知——避免在引号内部切断丢失对白。

    策略：若末段含开引号但未闭合，扩展到首个完整引号对结束；
    无引号则取末个终止标点之后的片段。
    """
    if not narration:
        return ""
    pairs = [("\u201c", "\u201d"), ("\u300c", "\u300d"), ('"', '"')]
    # 找最末的完整引号对
    # 直引号（op == cl）特殊处理：用 find 找最早的开引号位置，
    # 否则 rfind 会取到最末的闭引号位置导致配对失败
    last_pair_end = -1
    for op, cl in pairs:
        op_pos = narration.find(op) if op == cl else narration.rfind(op)
        if op_pos >= 0:
            cl_pos = narration.rfind(cl, op_pos + 1)
            if cl_pos >= 0 and cl_pos > last_pair_end:
                last_pair_end = cl_pos
    if last_pair_end >= 0:
        # 引号对之后的内容（通常是动作收尾）
        after_quote = narration[last_pair_end + 1:].strip()
        if after_quote:
            return after_quote
        # 引号对本身（含对白）作为末句
        return narration[max(0, last_pair_end - 60):last_pair_end + 1]
    # 无引号，取末个终止标点之后的片段
    matches = list(re.finditer(r"[。！？!?…；;]", narration))
    if matches:
        last = matches[-1]
        if last.end() < len(narration):
            return narration[last.end():].strip()
        # 末标点就是结尾，取该标点之前的整句
        prev = matches[-2] if len(matches) >= 2 else None
        start = prev.end() if prev else 0
        return narration[start:].strip()
    return narration


# 站位/朝向/姿态关键词
_BLOCKING_POSITION = re.compile(r"前|后|左|右|身旁|对面|相对|而立|远处|近处|身后|面前|并肩|对峙")
_BLOCKING_FACING = re.compile(r"面朝|背对|侧对|面向|背向|侧身|相对")
_BLOCKING_POSTURE = re.compile(r"站|坐|蹲|跪|躺|走|跑|倚|靠|盘膝|盘腿|而立")


def _infer_blocking(characters: list, narration: str, scene: str = "",
                    cast_map: dict = None) -> str:
    """
    从 narration 推断站位/朝向/姿态。

    输出"两人相对而立，间距约一臂"风格的短句。
    无明确站位信号时返回空字符串（不强行注入 blocking）。
    """
    if not narration:
        return ""

    n_chars = len(characters)
    pos_hit = _BLOCKING_POSITION.search(narration)
    facing_hit = _BLOCKING_FACING.search(narration)
    posture_hit = _BLOCKING_POSTURE.search(narration)

    # 多人镜头才能描述相对站位
    if n_chars >= 2 and pos_hit:
        # 站位词 + 姿态
        parts = []
        if facing_hit:
            parts.append(facing_hit.group(0))
        if posture_hit:
            parts.append(_posture_word(posture_hit.group(0)))
        if parts:
            return f"两人{pos_hit.group(0)}，{','.join(parts)}"
        return f"两人相对，间距约一臂"

    if n_chars == 1:
        # 单人镜头：描述朝向+姿态
        parts = []
        if facing_hit:
            parts.append(facing_hit.group(0))
        if posture_hit:
            parts.append(_posture_word(posture_hit.group(0)))
        if parts:
            return f"人物{','.join(parts)}，居中构图"
        return "人物居中站立"

    if n_chars == 0:
        # 0 人镜头：仅描述环境站位
        if posture_hit or facing_hit:
            return "空镜，环境定格"
        return ""

    # 3+ 人：群演虚化提示
    if n_chars > 3:
        return f"同框 {n_chars} 人（超 3 人上限），建议群演虚化突出主体"

    return ""


def _posture_word(w: str) -> str:
    """姿态词归一化（如"站"→"站立"，"坐"→"端坐"）。"""
    mapping = {
        "站": "站立", "坐": "端坐", "蹲": "蹲伏", "跪": "跪地",
        "躺": "横卧", "走": "行走", "跑": "奔跑",
        "倚": "倚靠", "靠": "倚靠", "盘膝": "盘膝", "盘腿": "盘腿",
    }
    return mapping.get(w, w)


# 关键词集合（用于衔接连贯性校验）
_CONTINUITY_KEYWORDS = re.compile(
    r"剑|刀|鞘|背影|回头|倒地|泪|怒|笑|惊|雨|雪|夜|晨|夕阳|"
    r"出鞘|入鞘|挥剑|收剑|睁眼|闭眼|低头|抬头|握拳"
)


def _keywords_overlap(a: str, b: str) -> bool:
    """判断两个衔接状态字符串是否有语义关键词重合。

    用于 validate_continuity：上一镜 exit_state 与本镜 entry_state 应有重合关键词，
    否则视为衔接不连贯。
    """
    if not a or not b:
        return True  # 任一为空时不触发告警（衔接信息缺失而非矛盾）
    a_words = set(_CONTINUITY_KEYWORDS.findall(a))
    b_words = set(_CONTINUITY_KEYWORDS.findall(b))
    if not a_words or not b_words:
        return True  # 都未匹配到关键词时放过
    return bool(a_words & b_words)


def _generic_identity(name: str, text: str, idx: int,
                      cast_map: dict = None, role: str = "video") -> str:
    """角色名 → 通用身份映射（避免视频正文中出现姓名污染）。

    优先级：
      1. cast_map 中该角色的 generic_identity.video_term / frame_term（结构化）
      2. 原文中的身份词（青年/女子/老者）+ 服饰/武器修饰（正则推断）
      3. 出场顺序分配"人物甲/乙/丙/丁"（兜底）

    role='video'：返回不含角色名的通用身份（投豆包视频正文）
    role='frame'：返回角色名+核心特征（投豆包分镜图，指向设定图）
    """
    # 路径1：cast_map 结构化取值
    if cast_map:
        c = cast_map.get(name)
        if c:
            gi = c.get("generic_identity") or {}
            if role == "frame":
                ft = gi.get("frame_term")
                if ft:
                    return ft
            else:
                # video 端：json 里预合成的 video_term 可能含坏拼接（逗号残缺、
                # 道具描述混入），有 persona 时现场重新合成更可靠
                persona = c.get("persona") or {}
                if persona:
                    return _synthesize_identity_from_persona(name, persona, role)
                vt = gi.get("video_term")
                if vt:
                    return vt
            # frame_term/video_term 缺失时从 persona 合成
            persona = c.get("persona") or {}
            if persona:
                return _synthesize_identity_from_persona(name, persona, role)

    # 路径2：原文正则推断（保留旧逻辑作为 fallback）
    pos = text.find(name)
    context = text[max(0, pos - 12): pos + len(name) + 20] if pos >= 0 else text
    weapon = ""
    for w in ("剑", "刀", "弓", "枪"):
        if w in context:
            weapon = ("持" + w) if w in ("剑", "刀", "弓", "枪") else ""
            break

    if role == "frame":
        # 分镜图端：保留角色名 + 上下文身份修饰
        if re.search(rf"青年{name}|{name}.*?青年", text):
            return f"{name}（{weapon}青年）" if weapon else f"{name}（青年）"
        if re.search(rf"(?:少女|女子|姑娘){name}|{name}.*?(?:少女|女子|姑娘)", text):
            color = "白" if "白" in context else ("黑" if "黑" in context else ("红" if "红" in context else ""))
            color_desc = f"{color}衣" if color else ""
            return f"{name}（{color_desc}女子）" if color_desc else f"{name}（女子）"
        if re.search(rf"(?:老者|老人){name}|{name}.*?(?:老者|老人)", text):
            return f"{name}（老者）"
        return name  # 分镜图端兜底保留原名（指向设定图）

    # 视频端：禁角色名，用通用身份
    if re.search(rf"青年{name}|{name}.*?青年|少年{name}|{name}.*?少年", text):
        return f"{weapon}青年" if weapon else "青年"
    if re.search(rf"(?:少女|女子|姑娘){name}|{name}.*?(?:少女|女子|姑娘)", text):
        if "白" in name or "白" in context:
            return "白衣女子"
        if "黑" in name or "黑" in context:
            return "黑衣女子"
        if "红" in name or "红" in context:
            return "红衣女子"
        return "女子"
    if re.search(rf"(?:老者|老人){name}|{name}.*?(?:老者|老人)", text):
        return "老者"
    if re.search(r"(?:将军|大人|王|侯|公)", context):
        return "将军"
    return _GENERIC_FALLBACK[idx] if idx < len(_GENERIC_FALLBACK) else f"人物{idx + 1}"


def _synthesize_identity_from_persona(name: str, persona: dict, role: str) -> str:
    """从结构化 persona 合成通用身份（无显式 generic_identity 时使用）。

    role='video'：合成"深灰长袍的持剑青年"风格（禁角色名）
    role='frame'：合成"苏见川（约30岁，剑眉星眸，深灰长袍）"风格（含角色名）
    """
    age = persona.get("age", "")
    gender = persona.get("gender", "")
    clothing = persona.get("clothing", "")
    hair = persona.get("hair", "")
    face = persona.get("face", "")
    props = persona.get("props", [])

    # 服饰颜色提取（用于 video 端简化）
    color = ""
    for c in ("白", "黑", "红", "青", "蓝", "灰", "绿", "紫", "黄"):
        if clothing and c in clothing:
            color = c
            break

    # 性别归一
    if gender.startswith("男"):
        gender_term = "青年" if age and ("25" in age or "30" in age or "20" in age) else "男子"
    elif gender.startswith("女"):
        gender_term = "女子"
    else:
        gender_term = "人物"

    if role == "video":
        # 禁角色名：服饰第一段 + 性别词，拼成"穿深灰色立领长风衣的男子"。
        # 道具不拼入（道具由道具参考图 + 画面内容传达），避免
        # "持半月形白玉佩，穿红丝穗男子"这类坏拼接。
        if not clothing:
            return gender_term or "人物"
        # 服饰取第一段（逗号/顿号前），并去掉开头穿戴动词
        clothing_first = re.split(r"[，,、]", clothing)[0].strip()
        clothing_first = re.sub(r"^(?:穿|披|裹|系|套|着|戴)", "", clothing_first)
        return f"穿{clothing_first}的{gender_term}" if gender_term else f"穿{clothing_first}的人物"
    else:
        # 含角色名：name + (age, face, clothing)
        info = []
        if age:
            info.append(age)
        if face:
            info.append(face)
        if clothing:
            info.append(clothing)
        return f"{name}（{', '.join(info[:3])}）" if info else name


def _split_outside_quotes(text: str, seps: str) -> list:
    """按 seps 中的分隔符切分文本，引号（“”‘’「」『』"'）内的分隔符不切分。

    返回去除分隔符后的片段列表（空片段丢弃）。
    直引号 " 和 ' 按翻转处理（成对出现）；弯引号/角引号按方向配对。
    """
    cjk_open = set("\u201c\u2018「『")
    cjk_close = set("\u201d\u2019」』")
    straight = set("\"'")
    enders = "。！？!?…"
    parts, buf, in_quote = [], [], False
    for ch in text:
        prev_state = in_quote
        if ch in straight:
            in_quote = not in_quote
            closing = prev_state  # 翻转前是开 → 本次是闭
        elif ch in cjk_open:
            in_quote = True
            closing = False
        elif ch in cjk_close:
            in_quote = False
            closing = True
        else:
            closing = False

        if ch in seps and not in_quote:
            if buf:
                seg = "".join(buf).strip()
                if seg:
                    parts.append(seg)
                buf = []
            continue

        buf.append(ch)
        # 台词句末：引号闭合且引号前是句末标点 → 在此断句（原文引号后常无句号）
        if closing and len(buf) >= 2 and buf[-2] in enders:
            seg = "".join(buf).strip()
            if seg:
                parts.append(seg)
            buf = []
    if buf:
        seg = "".join(buf).strip()
        if seg:
            parts.append(seg)
    return parts


def _split_beats(body: str, secs: int, dense: bool = False) -> list:
    """把画面内容拆成时间线节拍（普通镜每拍 3-6s，打斗镜每拍 2-4s）。

    策略：
      1. 先按句号/问号/感叹号拆句（引号内的标点不拆，避免台词被截断）
      2. 若段均时长超阈值（普通 6s / 打斗 3.5s），把含逗号的长句按逗号细拆：
         普通模式每 2 个子句合成一拍；打斗模式逐子句成拍，提高交锋节奏密度
      3. 兜底：拆不出多拍时返回原句单拍
    """
    sents = _split_outside_quotes(body, "。！？!?…")
    if not sents:
        return []
    threshold = 3.5 if dense else 6
    # 段均时长超阈值且句子够长时细拆
    if secs / len(sents) > threshold:
        fine = []
        for s in sents:
            clauses = _split_outside_quotes(s, "，,；;")
            if dense and len(clauses) >= 2 and len(s) >= 8:
                # 打斗：逐子句成拍（短拍快切是打斗节奏的来源）
                fine.extend(c for c in clauses if c)
            elif len(clauses) >= 2 and len(s) >= 12:
                # 至少 2 个子句且整句 >= 12 字才值得细拆（短尾句不阻塞，合并后自然补长）
                for j in range(0, len(clauses), 2):
                    pair = "，".join(clauses[j:j + 2])
                    if pair:
                        fine.append(pair)
            else:
                fine.append(s)
        sents = fine
    return sents


def _timeline_segments(secs: int, beats: list) -> list:
    """把 secs 均分给节拍列表，返回 [(start, end), ...]（秒）。"""
    n = len(beats)
    if n <= 0:
        return []
    seg = secs / n
    return [(int(i * seg), int((i + 1) * seg)) for i in range(n)]


# ── 打斗时间线指令库（动作片镜头语言）──
# 每条规则：(节拍文本正则, 运镜覆盖或 None, 画面增强描述)
# 第一个命中的规则生效；运镜为 None 时沿用 CAMERA_BY_BEAT["fight"] 的轮换项。
# 描述控制在 15 字左右的短句，避免提示词信息密度过高导致指令遵循不稳定。
FIGHT_BEAT_RULES = [
    (re.compile(r"撞开|破门|撞破|破门而入"),
     None, "木门爆裂，冲击瞬间木屑飞溅，冲击感强"),
    (re.compile(r"闯入|扑入|冲入|杀入"),
     None, "身影骤然闯入画面，气势凌厉"),
    (re.compile(r"拔剑|拔刀|出鞘|亮剑|抽刀|拔枪"),
     "低角度仰拍，快速推近", "兵器出鞘寒光暴闪，动作凌厉"),
    (re.compile(r"挥剑|挥刀|舞剑|挺枪|横扫|劈|砍|刺|斩|削|挑|撩|挥拳"),
     "低角度仰拍，打击点快速推近", "挥击轨迹带动态模糊残影，速度感强"),
    (re.compile(r"格挡|招架|挡下|架住|拦"),
     "打击点特写", "兵器碰撞迸出火花，金属震颤"),
    (re.compile(r"寒光|剑光|刀光|火花|火星|迸溅|飞溅|血光|血"),
     "打击点大特写", "寒光闪烁，火花迸溅"),
    (re.compile(r"闪|躲|避|跃|翻身|侧翻|后翻|翻滚"),
     "手持晃动紧跟动作", "快速闪避带动态模糊，衣袂翻飞"),
    (re.compile(r"击飞|打飞|踢飞|踹飞|击倒|倒地|倒下|跌落|坠|重创|吐血|踉跄"),
     "快速拉远再推近", "受击倒地，惯性前扑，打击感强"),
    (re.compile(r"拳|掌|爪|踢|腿|肘|膝"),
     "打击点快速推近特写", "拳脚交锋快切特写，打击感强"),
    (re.compile(r"开枪|扫射|射击|爆炸|轰|炮火"),
     None, "火光迸现，烟尘冲击，震撼感强"),
    (re.compile(r"对峙|僵持|凝视|锁定|逼近|围"),
     "缓慢环绕运镜", "剑拔弩张，张力紧绷"),
]


def _fight_beat_directives(beat_text: str, is_last: bool = False,
                           beats: list = None, idx: int = 0) -> tuple:
    """按打斗节拍文本匹配镜头语言指令，返回 (运镜覆盖或 None, 画面增强描述)。

    末拍追加慢动作特写：打斗高潮瞬间用慢动作定格是动作片的经典收尾节奏。
    beats/idx 用于跨拍语境判断（如"寒光"拍是否应出现火花）。
    """
    cam, desc = None, ""
    for pat, c, d in FIGHT_BEAT_RULES:
        if pat.search(beat_text):
            cam, desc = c, d
            break
    # 寒光/剑光规则：火花仅在双方兵器碰撞语境下出现；单方斩击改为"剑锋寒光划过"，
    # 避免"武器对碰后人倒地"的因果断裂。
    if desc == "寒光闪烁，火花迸溅":
        context = beat_text
        if beats:
            ctx_beats = [beats[j] for j in (idx - 1, idx, idx + 1)
                         if 0 <= j < len(beats)]
            context = "".join(ctx_beats)
        has_clash = bool(FIGHT_CLASH_RE.search(context))
        # 当前拍若直接写出"火花/火星/迸溅"，按用户原意保留火花
        explicit_spark = bool(re.search(r"火花|火星|迸溅", beat_text))
        if not has_clash and not explicit_spark:
            desc = "剑锋寒光划过"
    if is_last:
        desc = (desc + "，" if desc else "") + "瞬间慢动作特写"
    return cam, desc


def _fight_chain_desc(beat: str, beats: list, idx: int, weapon: str) -> str:
    """闭合打斗因果链：攻击拍点名攻击目标与命中部位，倒地拍写明被击中原因与失衡机制。

    返回附加到本拍描述中的短语（可为空）。仅对「攻击拍」和「受击结果拍」两类角色
    生效，其余拍（如寒光/闪避）不加，避免重复命中或语义冲突。
    """
    point = _fight_point_word(weapon)
    # 受击结果拍：给出"被什么击中 + 为何失衡"的因，供调用方前置到"倒地"结果之前，
    # 避免"先结果后原因"导致模型渲染成无来由的突然倒地（2026-10-20 用户反馈）。
    if FIGHT_RESULT_RE.search(beat):
        return f"被{point}命中，重心失衡"
    # 攻击拍：若其后 1~2 拍内出现受击结果拍，标注攻击目标与命中部位
    region = _STRIKE_REGIONS[idx % len(_STRIKE_REGIONS)]
    if FIGHT_ATTACK_RE.search(beat) and any(
            FIGHT_RESULT_RE.search(beats[j])
            for j in range(idx + 1, min(idx + 3, len(beats)))):
        return f"{point}直取对方，命中其{region}"
    return ""


def build_video_prompt(scene: str, narration: str, characters: list,
                       camera: str, style: str, ratio_desc: str, secs: int,
                       entry_state: str = "", blocking: str = "",
                       focal_length: str = "", camera_position: str = "",
                       composition: str = "", gaze_target: str = "",
                       focus_point: str = "", stability: str = "",
                       cast_map: dict = None, exit_state: str = "") -> str:
    """
    视频正文提示词（投豆包，禁角色名）。

    参考 shuohao-skills + Seedance 2.0 时间线分段法：
    把 secs 拆成若干时段，每段描述"画面内容 + 运镜"，让 10-15s 的视频
    有足够的情节密度，避免"十来秒只有一句画面"的空洞感。
    结构：画风 + 画幅时长 + 场景 + 时间线分段 + 站位 + 构图 + 光影 + 画质

    衔接+构图升级：
      * entry_state 注入首段时间线（承接上一镜结尾画面）
      * exit_state 注入末段时间线（末帧定格，衔接下一镜开头）
      * blocking 注入"站位：xxx" 段
      * 六构图字段合并注入"构图：xxx" 段
      * cast_map 驱动通用身份合成（无则退化为原文正则推断）
    """
    body = narration
    # 只对长度≥2 的角色名做替换（避免把单字道具如"剑"误替换）
    # 按长度倒序排序，避免短名先匹配
    replace_chars = sorted([c for c in characters if c and len(c) >= 2],
                           key=len, reverse=True)
    for i, name in enumerate(replace_chars):
        generic = _generic_identity(name, narration, i, cast_map=cast_map, role="video")
        body = body.replace(name, generic)
    # 去掉画面内容末尾的标点，避免与模板叠加
    body = re.sub(r"[。，,；;]+$", "", body.strip())

    parts = [f"{style}。{ratio_desc}，时长{secs}秒。场景：{scene}。"]

    # ── 时间线分段（每拍：时段 + 画面 + 运镜短语）──
    # 打斗镜：逐子句密排短拍 + 动作片镜头语言注入（打击感/速度感/受击反应/慢动作）
    is_fight = is_fight_scene(narration)
    fight_weapon = _fight_weapon_word(narration) if is_fight else ""
    beats = _split_beats(body, secs, dense=is_fight)
    if beats:
        segs = _timeline_segments(secs, beats)
        camera_terms = ([c.strip() for c in re.split(r"[，,]", camera) if c.strip()]
                        if camera else [])
        tl = []
        for i, ((start, end), beat) in enumerate(zip(segs, beats)):
            seg_text = f"{start}-{end}s：{beat}"
            if i == 0 and entry_state:
                seg_text += f"（开场承接上一镜结尾：{entry_state}）"
            cam_term = ""
            if is_fight:
                is_last_beat = (i == len(beats) - 1)
                rule_cam, desc = _fight_beat_directives(
                    beat, is_last=False, beats=beats, idx=i)
                # 因果链编排：攻击拍点名命中部位；倒地拍把"被命中+失衡"的因前置到
                # "倒地"结果之前，闭合"出招→命中→倒地"因果，避免受击倒地突兀。
                chain = _fight_chain_desc(beat, beats, i, fight_weapon)
                if chain:
                    if FIGHT_RESULT_RE.search(beat):
                        desc = chain + "，" + desc if desc else chain
                    else:
                        desc = (desc + "，" if desc else "") + chain
                # 末拍慢动作放在因果描述之后（先交代"怎么倒的"，再给收尾特写）
                if is_last_beat:
                    desc = (desc + "，" if desc else "") + "瞬间慢动作特写"
                if desc:
                    seg_text += f"，{desc}"
                if rule_cam:
                    cam_term = rule_cam
            if not cam_term and camera_terms:
                cam_term = camera_terms[i % len(camera_terms)]
            if cam_term:
                seg_text += f"。{cam_term}"
            tl.append(seg_text)
        # 末帧定格（exit_state）：收尾画面，同时是下一镜的衔接锚点
        if exit_state:
            tl.append(f"{secs}s定格：{exit_state}")
        parts.append("时间线：" + "。".join(tl) + "。")
        if is_fight:
            parts.append("打斗氛围：动作凌厉，打击感与速度感强，金属碰撞与破空音效清晰。")
    else:
        # 拆不出节拍时退化为整段描述
        if entry_state:
            parts.append(f"承接上一镜：{entry_state}。")
        parts.append(f"画面内容：{body}。")
    if blocking:
        parts.append(f"站位：{blocking}。")
    # 构图段：合并六字段
    comp_parts = []
    if focal_length:
        comp_parts.append(f"焦距{focal_length}")
    if camera_position:
        comp_parts.append(f"机位{camera_position}")
    if composition:
        comp_parts.append(f"构图{composition}")
    if gaze_target:
        comp_parts.append(f"视线{gaze_target}")
    if focus_point:
        comp_parts.append(f"焦点{focus_point}")
    if stability:
        comp_parts.append(f"稳定性{stability}")
    if comp_parts:
        parts.append("构图：" + "，".join(comp_parts) + "。")
    parts.append("光影：柔和自然光，体积光，轻微景深虚化。")
    parts.append("画质：4K超清，高细节，动态连贯，人物形象稳定不变形。")
    return "".join(parts)


def build_frame_prompt(scene: str, narration: str, characters: list,
                       camera: str, style: str, ratio_desc: str,
                       entry_state: str = "", blocking: str = "",
                       focal_length: str = "", camera_position: str = "",
                       composition: str = "", gaze_target: str = "",
                       focus_point: str = "", stability: str = "",
                       cast_map: dict = None) -> str:
    """
    分镜图提示词（关键帧图，直呼角色名）。

    参考 shuohao-skills：分镜图钉在 t=0.00s 主图钉位置，
    直呼角色名指向设定图（保证脸型一致），描述这一格的主体/位置/动作瞬间/光线氛围。

    衔接+构图升级：
      * entry_state 注入"承接上一镜末帧：xxx" 段（关键帧 t=0 必须承接上一镜末帧）
      * blocking 注入"站位：xxx" 段
      * 六构图字段合并注入"构图：xxx" 段
      * cast_map 驱动分镜图角色名+核心特征（指向设定图）
    """
    # 取 narration 第一句作为定格动作瞬间
    # 引号感知：避免在引号内部切断，丢失对白
    first_clause = _first_clause_quote_aware(narration)
    body = re.sub(r"[。，,；;]+$", "", first_clause.strip())

    # 分镜图端：直呼角色名 + cast_map frame_term 修饰
    if characters:
        char_list = "、".join(
            _generic_identity(name, narration, i, cast_map=cast_map, role="frame")
            for i, name in enumerate(characters)
        )
    else:
        # 2026-10-24 现场实测：空占位"画面主体"会让豆包自行补一个人物
        # （空巷镜生成了红衣女子，与原文"空无一人"矛盾）。改为明确空镜+排除条款。
        char_list = ("空镜，无任何人物与生物出现，画面中只有环境与氛围；"
                     "不要出现人脸、人体、剪影、倒影中的人")

    parts = [f"{style}。{ratio_desc}，定格关键帧（t=0.00s）。场景：{scene}。"]
    if entry_state:
        parts.append(f"承接上一镜末帧：{entry_state}。")
    parts.append(f"主体：{char_list}。")
    if blocking:
        parts.append(f"站位：{blocking}。")
    parts.append(f"动作瞬间：{body}。")
    # 构图段
    comp_parts = []
    if focal_length:
        comp_parts.append(f"焦距{focal_length}")
    if camera_position:
        comp_parts.append(f"机位{camera_position}")
    if composition:
        comp_parts.append(f"构图{composition}")
    if gaze_target:
        comp_parts.append(f"视线{gaze_target}")
    if focus_point:
        comp_parts.append(f"焦点{focus_point}")
    if stability:
        comp_parts.append(f"稳定性{stability}")
    if comp_parts:
        parts.append("构图：" + "，".join(comp_parts) + "。")
    parts.append(f"运镜基调：{camera}。")
    parts.append("光线：柔和自然光，体积光，眼神光清晰。")
    return "".join(parts)


def _last_clause_quote_aware(narration: str) -> str:
    """取末句，引号感知——避免在引号内部切断丢失对白。

    直引号 op==cl 时必须用 find（而非 rfind），否则取到最末引号位置导致配对失败。
    """
    pairs = [("“", "”"), ("「", "」"), ('"', '"')]
    for op, cl in pairs:
        op_pos = narration.find(op)
        if op_pos >= 0:
            cl_pos = narration.find(cl, op_pos + 1)
            if cl_pos >= 0:
                # 引号对结束位置之后的部分
                tail = narration[cl_pos + 1:]
                m = re.search(r"[。！？!?…；;]", tail)
                if m:
                    # 从引号对结束到第一个终止标点
                    return narration[cl_pos + 1:cl_pos + 1 + m.start() + 1]
                # 引号对后无终止标点，返回引号对本身
                return narration[op_pos:cl_pos + 1]
    # 无引号，取最后一个终止标点之后的句子
    m_iter = list(re.finditer(r"[。！？!?…；;]", narration))
    if m_iter:
        start = m_iter[-2].end() if len(m_iter) >= 2 else 0
        return narration[start:]
    return narration


def build_end_frame_prompt(scene: str, narration: str, characters: list,
                            exit_state: str, style: str, ratio_desc: str,
                            blocking: str = "",
                            focal_length: str = "", camera_position: str = "",
                            composition: str = "", gaze_target: str = "",
                            focus_point: str = "", stability: str = "",
                            cast_map: dict = None) -> str:
    """
    尾帧图提示词（v3 新增）。

    用途：描述本镜结束时的定格画面，用图生图模式生成（角色图+道具图作参考）。
    与 frame_prompt（首帧/t=0 定格）的区别：
      - frame_prompt 描述镜头开头的定格（entry_state 承接上一镜）
      - end_frame_prompt 描述镜头结尾的定格（exit_state 是本镜收尾状态）

    直呼角色名指向参考图，保证脸型一致性。
    生成的尾帧图作为下一镜首帧参考图的视觉锚点，同时上传给本镜视频生成作参考。
    """
    # 取 narration 末句作为定格动作瞬间
    last_clause = _last_clause_quote_aware(narration)
    body = re.sub(r"[。，,；;]+$", "", last_clause.strip())

    # 角色列表：直呼角色名 + cast_map frame_term 修饰（指向设定图）
    if characters:
        char_list = "、".join(
            _generic_identity(name, narration, i, cast_map=cast_map, role="frame")
            for i, name in enumerate(characters)
        )
    else:
        # 2026-10-24 现场实测：同 build_frame_prompt，空镜必须显式排除人物
        char_list = ("空镜，无任何人物与生物出现，画面中只有环境与氛围；"
                     "不要出现人脸、人体、剪影、倒影中的人")

    parts = [f"{style}。{ratio_desc}，尾帧定格画面（t=结束）。场景：{scene}。"]
    parts.append(f"主体：{char_list}。")
    if blocking:
        parts.append(f"站位：{blocking}。")
    # 尾帧核心：exit_state（本镜结束时的定格状态）
    if exit_state:
        parts.append(f"结束状态：{exit_state}。")
    parts.append(f"定格动作：{body}。")
    # 构图段
    comp_parts = []
    if focal_length:
        comp_parts.append(f"焦距{focal_length}")
    if camera_position:
        comp_parts.append(f"机位{camera_position}")
    if composition:
        comp_parts.append(f"构图{composition}")
    if gaze_target:
        comp_parts.append(f"视线{gaze_target}")
    if focus_point:
        comp_parts.append(f"焦点{focus_point}")
    if stability:
        comp_parts.append(f"稳定性{stability}")
    if comp_parts:
        parts.append("构图：" + "，".join(comp_parts) + "。")
    parts.append("光线：与镜头结尾氛围一致，体积光，眼神光清晰。")
    parts.append("高质量定格画面，人物形象与参考图一致，细节清晰。")
    return "".join(parts)


def _first_clause_quote_aware(narration: str) -> str:
    """取首句，引号感知——避免在引号内部切断丢失对白。

    策略：若首句含开引号但未闭合，扩展到首个完整引号对结束。
    """
    # 直引号和弯引号统一处理
    pairs = [("“", "”"), ("「", "」"), ('"', '"')]
    for op, cl in pairs:
        op_pos = narration.find(op)
        if op_pos >= 0:
            cl_pos = narration.find(cl, op_pos + 1)
            if cl_pos >= 0:
                # 引号在前面，找到引号对结束的位置
                head = narration[:op_pos]
                # head 中若已有终止标点，按标点切（引号前的引导句）
                m = re.search(r"[。！？!?…；;]", head)
                if m:
                    return head[:m.start() + 1]
                # 否则整段（含引号对）作为首句
                return narration[:cl_pos + 1]
    # 无引号，取到首个终止标点
    m = re.search(r"[。！？!?…；;]", narration)
    if m:
        return narration[:m.start() + 1]
    return narration


def _extract_shot_props(narration: str, characters: list, cast_map: dict = None,
                        props_defs: list = None) -> list:
    """提取单镜出场道具。

    策略（无道具时返回空列表，不强制提取）：
      1. 从出场角色的 persona.props 中获取该角色绑定的道具名
      2. 从旁白文本中搜索道具名词（剑/刀/玉佩/灯笼…）
      3. 用 props_defs（characters.json 的 props 列表，含 name+aliases）规范化命名
      4. 合并去重，按出现顺序保序
    """
    raw_props = []
    seen = set()

    # 1. 角色绑定道具（来自 characters.json 的 persona.props）
    if cast_map:
        for name in characters:
            char = cast_map.get(name)
            if not char:
                continue
            for p in (char.get("persona", {}) or {}).get("props", []) or []:
                core = _match_prop_noun(p)
                if core and core not in seen:
                    seen.add(core)
                    raw_props.append(core)

    # 2. 旁白文本中出现的道具名词
    for m in _PROP_NOUNS.finditer(narration):
        tok = m.group(0)
        if tok not in ("玉", "佩") and tok not in seen:
            seen.add(tok)
            raw_props.append(tok)

    # 3. 用 props_defs 规范化命名（别名统一为正式名）
    if props_defs:
        # 构建别名 -> 正式名 映射
        alias_map = {}
        for pd in props_defs:
            formal = pd.get("name", "")
            if not formal:
                continue
            alias_map[formal] = formal  # 自己也映射自己
            for alias in pd.get("aliases", []) or []:
                alias_map[alias] = formal
        # 规范化：如果能匹配到正式名，用正式名；否则保留原名
        normalized = []
        nseen = set()
        for p in raw_props:
            formal = alias_map.get(p, p)
            if formal not in nseen:
                nseen.add(formal)
                normalized.append(formal)
        return normalized

    return raw_props


# 道具名词表（与 extract_characters.py 保持一致，独立副本避免跨模块耦合）
_PROP_NOUNS = re.compile(
    r"(?:长剑|短剑|佩剑|长剑|剑|刀|枪|棍|杖|扇|伞|笛|箫|琴|弓|弩|盾|斧|锤|鞭|锏|"
    r"玉佩|玉|佩|簪|钗|链|珠|符|印|令牌|信|信件|书|卷|册|"
    r"灯笼|灯|烛|香|丹|药|瓶|葫芦|袋|囊|箱|匣|"
    r"面具|斗笠|蓑衣|披风|斗篷)"
)


def _match_prop_noun(text: str) -> str:
    """从描述性片段中提取核心道具名（最长匹配优先）。"""
    matches = list(_PROP_NOUNS.finditer(text))
    if not matches:
        return ""
    # 取最长的匹配（如"长剑"优先于"剑"）
    matches.sort(key=lambda m: len(m.group(0)), reverse=True)
    return matches[0].group(0)


def _finalize_prompts(sl: ShotList, cast_map: dict = None, props_defs: list = None) -> None:
    """角色标注完成后，用 shot.characters 重新生成 prompt/frame_prompt。

    build_shot 阶段无 characters 信息，prompt 暂未做角色名替换；
    本函数在 annotate_characters 之后调用，确保双提示词分工最终落地。

    升级：把衔接字段+构图字段+cast_map 一并传入，确保最终 prompt 含全部新字段。
    同时回填 on_screen_count（同框人数）和 props（出场道具）。
    props_defs：characters.json 的 props 列表（含 name+aliases），用于道具名规范化。
    """
    for s in sl.shots:
        # 同框人数回填
        s.on_screen_count = len(s.characters)
        # 出场道具回填（无道具则为空列表）
        s.props = _extract_shot_props(s.narration, s.characters, cast_map, props_defs)
        # blocking 用真实 characters 重新推断（build_shot 阶段 characters 还为空）
        if s.blocking is None or s.blocking == "":
            s.blocking = _infer_blocking(s.characters, s.narration, s.scene, cast_map)

        s.prompt = build_video_prompt(
            s.scene, s.narration, s.characters, s.camera, s.style, s.ratio, s.seconds,
            entry_state=s.entry_state, blocking=s.blocking,
            focal_length=s.focal_length, camera_position=s.camera_position,
            composition=s.composition, gaze_target=s.gaze_target,
            focus_point=s.focus_point, stability=s.stability,
            cast_map=cast_map, exit_state=s.exit_state,
        )
        s.frame_prompt = build_frame_prompt(
            s.scene, s.narration, s.characters, s.camera, s.style, s.ratio,
            entry_state=s.entry_state, blocking=s.blocking,
            focal_length=s.focal_length, camera_position=s.camera_position,
            composition=s.composition, gaze_target=s.gaze_target,
            focus_point=s.focus_point, stability=s.stability,
            cast_map=cast_map,
        )
        # v3：尾帧图提示词（用 exit_state 而非 entry_state）
        s.end_frame_prompt = build_end_frame_prompt(
            s.scene, s.narration, s.characters, s.exit_state, s.style, s.ratio,
            blocking=s.blocking,
            focal_length=s.focal_length, camera_position=s.camera_position,
            composition=s.composition, gaze_target=s.gaze_target,
            focus_point=s.focus_point, stability=s.stability,
            cast_map=cast_map,
        )


# ──────────────────────────────────────────────────────────────
# 分集与悬念切分
# ──────────────────────────────────────────────────────────────

def shot_tension(text: str, beat: str) -> float:
    """镜头张力评分——决定它有没有资格当一集的结尾。"""
    score = BEAT_TENSION.get(beat, 1.0)
    if re.search(r"[！!？?]", text):
        score += 0.5      # 惊呼/追问比平铺直叙更有钩子
    return score


EPISODE_MARK = {
    "hook": "🪝 结尾留悬念",
    "resolve": "🎬 正常收尾",
    "carry_over": "⏭ 下集开场承接",
    "flat": "⚠️ 结尾偏平，需人工处理",
}
EPISODE_MARK_SHORT = {
    "hook": "结尾留悬念",
    "resolve": "正常收尾",
    "carry_over": "下集开场承接",
    "flat": "结尾偏平",
}


def episode_summary(shots: list) -> list:
    """把镜头按 episode 字段汇总成集信息列表（含结尾类型 kind）。"""
    eps = []
    for s in shots:
        ei = getattr(s, "episode", 0) or 1
        if not eps or eps[-1]["index"] != ei:
            eps.append({
                "index": ei, "shot_start": s.index, "shot_end": s.index,
                "shot_count": 0, "seconds": 0, "ends_on_hook": False,
                "carry_over": False, "kind": "hook", "_carry_n": 0, "_flat_n": 0,
            })
        e = eps[-1]
        e["shot_end"] = s.index
        e["shot_count"] += 1
        e["seconds"] += s.seconds
        e["ends_on_hook"] = bool(getattr(s, "hook_end", False))
        e["_carry_n"] += 1 if getattr(s, "carry_over", False) else 0
        e["_flat_n"] += 1 if getattr(s, "flat_end", False) else 0

    for e in eps:
        # 整集都由上一集顺延而来 → 它是下一集的开场承接段，不是独立成集
        full_carry = e["_carry_n"] > 0 and e["_carry_n"] == e["shot_count"]
        e["carry_over"] = full_carry
        if full_carry:
            e["ends_on_hook"] = False
            e["kind"] = "carry_over"
        elif e["ends_on_hook"]:
            e["kind"] = "hook"
        elif e["_flat_n"]:
            e["kind"] = "flat"
        else:
            e["kind"] = "resolve"
        e.pop("_carry_n")
        e.pop("_flat_n")
    return eps


def assign_episodes(shots: list, ep_size: int, is_final: bool) -> list:
    """
    分集 + 悬念切分，返回 warning 列表。

    规则（来自微短剧行业惯例：钩子在前，结尾切在高潮未解决处）：
      * 每集最多 ep_size 镜（默认 5 镜 ≈ 60 秒）
      * 除「完结章的最后一集」外，每集结尾必须停在精彩之处：
        若该集尾部是低张力镜头或含归位词（"终于/从此/恢复平静"），
        则把它顺延到下一集开头，使本集结尾落回高潮段——即"精彩部分过半之后"切断
      * 顺延后本集至少保留 MIN_EPISODE_SHOTS 镜（2 镜），不把整集推空
    """
    warnings = []
    if not shots:
        return warnings
    size = ep_size if ep_size and ep_size > 0 else len(shots)

    groups = [list(range(i, min(i + size, len(shots)))) for i in range(0, len(shots), size)]
    carried = set()      # 由上一集顺延过来的"下集开场"镜头

    # 从最后一集往前调整，避免移动影响后续判断
    for gi in range(len(groups) - 1, -1, -1):
        if gi == len(groups) - 1 and is_final:
            continue                      # 完结章的最后一集：正常收尾
        g = groups[gi]
        # 下一集还能再塞几镜；末集被砍下的尾巴另起一集，容量按 size 算
        room = size - len(groups[gi + 1]) if gi + 1 < len(groups) else size
        tail = []
        # 本集至少保留 MIN_EPISODE_SHOTS 镜；最多只能顺延下一集装得下的量
        while len(g) > MIN_EPISODE_SHOTS and len(tail) < room:
            s = shots[g[-1]]
            txt = s.source_text or s.narration
            if shot_tension(txt, s.beat) >= HOOK_TENSION and not RESOLVE_RE.search(txt):
                break                     # 已是高张力镜头，可作悬念结尾
            tail.append(g.pop())
        if not tail:
            continue
        tail.reverse()
        if gi + 1 < len(groups):
            groups[gi + 1][:0] = tail
        else:
            groups.append(tail)
            warnings.append(
                f"第 {gi + 1} 集结尾属于收束内容，已将最后 {len(tail)} 镜顺延为第 {gi + 2} 集开场"
                "（该集是下一集的开头，可与下一章内容合并成集）"
            )
        carried.update(tail)

    for ei, g in enumerate(groups, 1):
        last_ep = ei == len(groups)
        for pos, si in enumerate(g):
            s = shots[si]
            s.episode = ei
            s.episode_end = pos == len(g) - 1
            s.carry_over = si in carried
            # 需要留悬念的集：非末集，或连载中章节的末集；承接段本身不算
            needs_hook = bool(s.episode_end and (not last_ep or not is_final)
                              and not s.carry_over)
            txt = s.source_text or s.narration
            s.hook_end = bool(needs_hook
                              and shot_tension(txt, s.beat) >= HOOK_TENSION
                              and not RESOLVE_RE.search(txt))
            if needs_hook and not s.hook_end:
                s.flat_end = True
                warnings.append(
                    f"第 {ei} 集结尾（镜 {s.index}）是平缓/收束内容，且本集镜头不足以再切出悬念；"
                    "建议与相邻集合并，或人工补一个钩子镜头"
                )
    return warnings


# ──────────────────────────────────────────────────────────────
# 两种输入源
# ──────────────────────────────────────────────────────────────

def guess_title(text: str, fallback: str) -> str:
    """
    从正文推断作品标题。

    优先取文件首个标题行；没有标题行则用文件名兜底。
    """
    for line in text.split("\n"):
        s = line.strip()
        if s and is_title_line(s):
            t = re.sub(r"^#{1,6}\s*", "", s)
            t = re.sub(r"^第[一二三四五六七八九十百千零\d]+[章节回卷篇部]\s*", "", t).strip()
            if t:
                return t
    return fallback


def from_novel(text: str, title: str, style_key: str, ratio: str, model: str,
               ep_size: int = EPISODE_SHOTS, is_final: bool = False,
               cast_map: dict = None, no_continuity: bool = False) -> ShotList:
    """从小说章节正文拆解。

    cast_map（可选）：结构化角色圣经，驱动通用身份合成。
    no_continuity=True：跳过衔接字段注入（独立单镜/调试场景）。
    """
    style = STYLE_PRESETS.get(style_key, STYLE_PRESETS["3d-realistic"])
    ratio_desc = RATIO_MAP.get(ratio, RATIO_MAP["16:9"])

    sentences = split_sentences(text)
    groups = greedy_group(sentences)

    # 场景延续：上一镜的场景作为下一镜的候选，避免每镜都写"故事主场景"
    # 衔接：上一镜对象作为本镜 prev_shot，让 entry_state 承接 exit_state
    shots, prev_scene, prev_shot = [], "", None
    for i, (t, s) in enumerate(groups):
        shot = build_shot(i + 1, t, s, style, ratio_desc, prev_scene,
                          prev_shot=prev_shot, cast_map=cast_map,
                          no_continuity=no_continuity)
        prev_scene = shot.scene
        prev_shot = shot
        shots.append(shot)

    sl = ShotList(
        title=title, style_key=style_key, ratio=ratio,
        style_desc=style, ratio_desc=ratio_desc, model=model,
        shots=shots, source_type="novel", total_chars=count_cjk(text),
        episode_shots=ep_size, is_final=is_final,
    )
    sl.warnings.extend(assign_episodes(shots, ep_size, is_final))
    if not shots:
        sl.warnings.append("未能从输入中解析出有效正文，请检查文件内容。")
    sl.warnings.extend(validate_continuity(sl.shots))
    return sl


def from_concept(plan: dict, style_key: str, ratio: str, model: str,
                 ep_size: int = EPISODE_SHOTS, is_final: bool = False,
                 cast_map: dict = None, no_continuity: bool = False) -> ShotList:
    """
    从故事梗概/描述拆解（非小说章节场景）。

    plan 结构：
    {
      "title": "故事标题",
      "logline": "一句话故事",
      "characters": [{"name":"...", "desc":"..."}],
      "setting": "世界观与场景设定",
      "beats": [
        {"text": "情节描述", "seconds": 15, "note": "导演备注"},
        ...
      ]
    }

    cast_map（可选）：结构化角色圣经，驱动通用身份合成。
    no_continuity=True：跳过衔接字段注入（独立单镜/调试场景）。
    """
    style = STYLE_PRESETS.get(style_key, STYLE_PRESETS["3d-realistic"])
    ratio_desc = RATIO_MAP.get(ratio, RATIO_MAP["16:9"])

    # plan.characters 转为 cast_map（兼容旧版 desc-only schema）
    plan_chars = plan.get("characters", [])
    cast_map = cast_map or {}
    for c in plan_chars:
        if c.get("name") and c["name"] not in cast_map:
            cast_map[c["name"]] = c

    char_brief = ""
    if plan_chars:
        char_brief = "角色设定：" + "；".join(
            f"{c.get('name','角色')}{c.get('desc','')}" for c in plan_chars[:4]
        ) + "。"

    shots = []
    prev_scene = plan.get("setting", "") or ""
    prev_shot = None
    for i, beat in enumerate(plan.get("beats", [])):
        text = re.sub(r"\s+", " ", beat.get("text", "")).strip()
        if not text:
            continue
        sec = beat.get("seconds") or estimate_seconds(text, detect_beat(text))
        secs = max(MIN_SHOT_SECONDS, min(MAX_SHOT_SECONDS, int(round(sec))))
        b = detect_beat(text)
        camera = beat.get("camera") or CAMERA_BY_BEAT.get(b, CAMERA_BY_BEAT["narration"])
        scene = beat.get("scene") or extract_scene(text, prev_scene)

        # 衔接+构图字段
        comp = COMPOSITION_BY_BEAT.get(b, COMPOSITION_BY_BEAT["narration"])
        if no_continuity:
            entry_state, exit_state, blocking = "", "", ""
        else:
            if prev_shot and prev_shot.exit_state:
                entry_state = prev_shot.exit_state
            elif prev_shot:
                entry_state = _infer_state_from_tail(prev_shot.narration, prev_shot.scene, is_exit=True)
            else:
                entry_state = ""
            exit_state = _infer_state_from_tail(text, scene, is_exit=True)
            blocking = _infer_blocking([], text, scene, cast_map)

        prompt = build_video_prompt(
            scene, text, [], camera, style, ratio_desc, secs,
            entry_state=entry_state, blocking=blocking,
            focal_length=comp["focal_length"], camera_position=comp["camera_position"],
            composition=comp["composition"], gaze_target=comp["gaze_target"],
            focus_point=comp["focus_point"], stability=comp["stability"],
            cast_map=cast_map, exit_state=exit_state,
        )
        frame = build_frame_prompt(
            scene, text, [], camera, style, ratio_desc,
            entry_state=entry_state, blocking=blocking,
            focal_length=comp["focal_length"], camera_position=comp["camera_position"],
            composition=comp["composition"], gaze_target=comp["gaze_target"],
            focus_point=comp["focus_point"], stability=comp["stability"],
            cast_map=cast_map,
        )
        narration = beat.get("narration", text)
        prev_scene = scene
        shot = Shot(
            index=i + 1, seconds=secs, beat=b, beat_label=BEAT_LABEL.get(b, b),
            scene=scene, action=text[:60], camera=camera,
            narration=narration, source_text=text,
            style=style, ratio=ratio_desc, prompt=prompt, frame_prompt=frame,
            blocking=blocking, entry_state=entry_state, exit_state=exit_state,
            focal_length=comp["focal_length"], camera_position=comp["camera_position"],
            composition=comp["composition"], gaze_target=comp["gaze_target"],
            focus_point=comp["focus_point"], stability=comp["stability"],
        )
        shots.append(shot)
        prev_shot = shot

    sl = ShotList(
        title=plan.get("title", "未命名漫剧"),
        style_key=style_key, ratio=ratio, style_desc=style,
        ratio_desc=ratio_desc, model=model, shots=shots,
        source_type="concept", total_chars=count_cjk(json.dumps(plan, ensure_ascii=False)),
        episode_shots=ep_size, is_final=is_final,
    )
    sl.warnings.extend(assign_episodes(shots, ep_size, is_final))
    sl.warnings.append("当前为「故事描述」模式：分镜由梗概派生，若剧情不足请先补充 beats。")
    sl.warnings.extend(validate_continuity(sl.shots))
    return sl


# ──────────────────────────────────────────────────────────────
# 质量门（轻量版，参考 shuohao-skills 18 道门）
# ──────────────────────────────────────────────────────────────

def validate_continuity(shots: list) -> list:
    """衔接连贯性校验：上一镜 exit_state → 本镜 entry_state 关键词重合。

    同场景连续镜头，blocking 的人物站位不得矛盾。
    返回 warnings 列表（不挂到 sl.warnings，由调用方决定）。
    """
    warnings = []
    for i in range(1, len(shots)):
        prev, cur = shots[i - 1], shots[i]
        # 门1：衔接关键词重合（除非任一为空）
        if prev.exit_state and cur.entry_state:
            if not _keywords_overlap(prev.exit_state, cur.entry_state):
                warnings.append(
                    f"镜 {cur.index} 衔接不连贯：上一镜收尾 '{prev.exit_state[:20]}'，"
                    f"本镜承接 '{cur.entry_state[:20]}'（无重合语义关键词）"
                )
        # 门2：同场景连续镜头 blocking 矛盾检测
        # （前镜"两人相对" → 后镜"两人同向"应警告，但当前 _infer_blocking 不会产生矛盾输出，留作占位）
    return warnings


def validate_shots(sl: "ShotList") -> list:
    """5 道核心质量门：单镜时长/台词装得下/同框≤3/构图字段齐全/衔接连贯。

    返回 warnings 列表。
    """
    warnings = []
    for i, s in enumerate(sl.shots):
        # 门1：单镜时长 ≤ 15s（豆包硬约束）
        if s.seconds > MAX_SHOT_SECONDS:
            warnings.append(f"镜 {s.index} 时长 {s.seconds}s 超过豆包 {MAX_SHOT_SECONDS}s 上限")
        # 门2：台词装得下（按字数/秒数）
        est = count_cjk(s.narration) / CHARS_PER_SEC + VISUAL_PADDING
        if est > s.seconds + 0.5:
            warnings.append(
                f"镜 {s.index} 旁白字数 {count_cjk(s.narration)} 字，"
                f"朗读需 {est:.1f}s 超过镜头 {s.seconds}s"
            )
        # 门3：同框≤3人
        if s.on_screen_count > 3:
            warnings.append(
                f"镜 {s.index} 同框 {s.on_screen_count} 人，建议拆镜或加群演虚化"
            )
        # 门4：衔接连贯（上一镜 exit_state → 本镜 entry_state 关键词重合）
        if i > 0:
            prev = sl.shots[i - 1]
            if prev.exit_state and s.entry_state:
                if not _keywords_overlap(prev.exit_state, s.entry_state):
                    warnings.append(
                        f"镜 {s.index} 衔接不连贯：上一镜收尾 '{prev.exit_state[:20]}'，"
                        f"本镜承接 '{s.entry_state[:20]}'"
                    )
        # 门5：构图六字段齐全
        missing = [f for f in ("focal_length", "camera_position", "composition",
                               "gaze_target", "focus_point", "stability")
                   if not getattr(s, f, "")]
        if missing:
            warnings.append(f"镜 {s.index} 缺构图字段：{','.join(missing)}")
    return warnings


# ──────────────────────────────────────────────────────────────
# 输出
# ──────────────────────────────────────────────────────────────

def render_markdown(sl: ShotList) -> str:
    """输出人可读的 Markdown 分镜表。"""
    L = []
    eps = episode_summary(sl.shots)
    L.append(f"# {sl.title} · 漫剧分镜表\n")
    L.append(f"> 风格：{sl.style_desc[:28]}… | 比例：{sl.ratio} | 模型：{sl.model}")
    L.append(f"> 镜头数：**{len(sl.shots)}** | 总时长：**{sum(s.seconds for s in sl.shots)}s** "
             f"| 单镜上限：{MAX_SHOT_SECONDS}s | 预计额度消耗：{sum(s.total_cost for s in sl.shots)}\n")
    if eps:
        tail = "完结章：末集正常收尾" if sl.is_final else "连载中：每集结尾停在悬念处"
        scope = f"每集 ≤ {sl.episode_shots} 镜" if sl.episode_shots else "不分集"
        L.append(f"> 集数：**{len(eps)}** | {scope} | {tail}\n")
        for e in eps:
            L.append(f"> - 第 {e['index']} 集：镜头 {e['shot_start']:02d}–{e['shot_end']:02d} · "
                     f"{e['shot_count']} 镜 · {e['seconds']}s · {EPISODE_MARK[e['kind']]}")
        L.append("")
    L.append("> ⚠️ 豆包免费版单次生成最长 15 秒，因此每个镜头必须分次单独生成。\n")
    L.append("---\n")

    for s in sl.shots:
        L.append(f"## 镜头 {s.index:02d} · {s.seconds}s · {getattr(s,'beat_label',s.beat)}\n")
        # 表格行之间不能有空行，否则 Markdown 渲染断裂
        L.append("| 项 | 内容 |")
        L.append("|---|---|")
        L.append(f"| 场景 | {s.scene} |")
        L.append(f"| 节拍 | {getattr(s,'beat_label',s.beat)} |")
        L.append(f"| 运镜 | {s.camera} |")
        if getattr(s, "focal_length", ""):
            L.append(f"| 焦距 | {s.focal_length} |")
            L.append(f"| 机位 | {s.camera_position} |")
            L.append(f"| 构图 | {s.composition} |")
            L.append(f"| 视线 | {s.gaze_target} |")
            L.append(f"| 焦点 | {s.focus_point} |")
            L.append(f"| 稳定性 | {s.stability} |")
        if getattr(s, "blocking", ""):
            L.append(f"| 站位 | {s.blocking} |")
        if getattr(s, "entry_state", ""):
            L.append(f"| 承接上一镜 | {s.entry_state} |")
        if getattr(s, "exit_state", ""):
            L.append(f"| 收尾状态 | {s.exit_state} |")
        on_screen = getattr(s, "on_screen_count", 0)
        if on_screen:
            flag = " ⚠️超3人" if on_screen > 3 else ""
            L.append(f"| 同框人数 | {on_screen}{flag} |")
        L.append(f"| 旁白/台词 | {s.narration} |\n")
        L.append(f"**分镜图提示词**（t=0.00s 关键帧，直呼角色名指向设定图）：\n")
        L.append("```")
        L.append(s.frame_prompt)
        L.append("```\n")
        L.append(f"**投喂豆包的视频正文**（禁角色名，用通用身份避免姓名污染）：\n")
        L.append("```")
        L.append(s.prompt)
        L.append("```\n")
        L.append(f"<details><summary>对应原文</summary>\n{s.source_text}\n</details>\n")
        if getattr(s, "episode_end", False):
            if s.carry_over:
                note = (f"> ⏭ **第 {s.episode} 集开场段结束** —— 本集由上一集顺延而来，"
                        "是下一集的开场承接，可与下一章合并成集")
            elif s.hook_end:
                note = (f"> 🪝 **第 {s.episode} 集结束于此** —— 停在高潮未解决处，"
                        "把解答留给下一集开场")
            elif s.flat_end:
                note = (f"> ⚠️ **第 {s.episode} 集结尾偏平** —— 应为悬念却落在收束内容上，"
                        "建议合并到相邻集或人工补一个钩子镜头")
            else:
                note = f"> 🎬 **第 {s.episode} 集结束** —— 正常收尾"
            L.append(note + "\n")
        L.append("---\n")
    return "\n".join(L)


def render_prompts_txt(sl: ShotList) -> str:
    """输出逐镜纯提示词文件，便于复制粘贴。"""
    eps = episode_summary(sl.shots)
    blocks = []
    for s in sl.shots:
        tag = ""
        if getattr(s, "episode_end", False):
            if s.carry_over:
                tag = f" | 第{s.episode}集末·承接"
            elif s.hook_end:
                tag = f" | 第{s.episode}集末·悬念"
            elif s.flat_end:
                tag = f" | 第{s.episode}集末·偏平"
            else:
                tag = f" | 第{s.episode}集末"
        blocks.append(
            f"===== 镜头 {s.index:02d} | {s.seconds}s | {getattr(s,'beat_label',s.beat)}{tag} =====\n{s.prompt}"
        )
    header = (
        f"# {sl.title} — 逐镜提示词（{len(sl.shots)} 镜 / {len(eps)} 集）\n"
        f"# 逐条粘贴到豆包视频生成，每次一条，等渲染完成再发下一条\n"
        f"# 注意：第N集末的镜头是悬念切点，不要往后接，剩余内容属于下一集\n"
    )
    return header + "\n" + "\n\n".join(blocks) + "\n"


def render_voiceover(sl: ShotList) -> str:
    """输出配音文案 + SRT 字幕，供剪映/PR 后期使用。"""
    eps = episode_summary(sl.shots)
    lines = [f"# {sl.title} — 配音文案\n"]
    lines.append("## 逐镜口播稿\n")
    for s in sl.shots:
        tag = ""
        if getattr(s, "episode_end", False):
            if s.carry_over:
                end_kind = "承接"
            elif s.hook_end:
                end_kind = "悬念"
            elif s.flat_end:
                end_kind = "偏平"
            else:
                end_kind = "收尾"
            tag = f" · 第{s.episode}集末（{end_kind}）"
        lines.append(f"**镜头 {s.index:02d}**（{s.seconds}s{tag}）\n")
        lines.append(f"> {s.narration}\n")

    # 生成 SRT：按集分段，每集时间轴从 0 重新计时（对应该集独立成片）
    lines.append("\n## 字幕文件（SRT，可直接导入剪映）\n")
    lines.append("每集独立一段 SRT，时间轴从该集第一镜重新计时，按集成片时直接对应。\n")
    for e in eps:
        group = [s for s in sl.shots if (s.episode or 1) == e["index"]]
        lines.append(f"### 第 {e['index']} 集（{e['shot_count']} 镜 / {e['seconds']}s）\n")
        lines.append("```srt")
        idx, t = 1, 0.0
        for s in group:
            start, end = t, t + s.seconds
            lines.append(str(idx))
            lines.append(f"{_srt_ts(start)} --> {_srt_ts(end)}")
            lines.append(s.narration if len(s.narration) <= 40 else s.narration[:38] + "…")
            lines.append("")
            idx += 1
            t = end
        lines.append("```\n")
    return "\n".join(lines)


def _srt_ts(sec: float) -> str:
    sec = int(sec)
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h:02d}:{m:02d}:{s:02d},000"


def annotate_characters(sl: ShotList, characters_json: str | None):
    """
    标注每个镜头的出场角色。

    如果提供了 characters_json（来自 extract_characters.py 或手写），
    则从中读取角色名列表 + cast_map（结构化角色圣经）；
    否则从全文中自动检测。然后扫描每个镜头的 source_text，匹配出场角色。

    cast_map 驱动 _generic_identity 的通用身份合成（无 cast_map 时退化到原文正则推断）。
    """
    char_names = []
    cast_map = {}
    props_defs = []  # characters.json 中的道具定义（含 name+aliases）

    if characters_json:
        try:
            with open(characters_json, encoding="utf-8") as f:
                data = json.load(f)
            for c in data.get("characters", []):
                if c.get("name"):
                    char_names.append(c["name"])
                    cast_map[c["name"]] = c
            props_defs = data.get("props", []) or []
        except (FileNotFoundError, json.JSONDecodeError, KeyError) as e:
            print(f"⚠️  读取角色定义失败({e})，改为自动检测")

    if not char_names:
        # 自动检测：从已拆分的镜头原文中提取高频人名
        all_text = " ".join(s.source_text for s in sl.shots)
        # 策略1：对话归属 "X道：" 识别候选
        # 副词列表含"颤声/哑声/冷声/急声/沉声/厉声/喝/呵/呢喃"等"X声/单字动词"变体
        dialogue_names = set()
        for m in re.finditer(
            r"([\u4e00-\u9fa5]{2,3})(?:[一-鿿]{0,2}声|缓缓|低声|高声|冷冷|淡淡|轻轻|静静|猛地|忽然|突然|默默|沉|厉|急|冷|喝|呵|叹|笑)?"
            r"(?:道|说|问|答|笑|喊|骂|叹|喝|呵|啐)(?:[：:，,])", all_text
        ):
            name = m.group(1)
            if not re.match(r"^[他她它你我妳您这那此某其]", name) \
               and not re.search(r"[的了着地将把被对从在于向以为给让]", name) \
               and name not in ("缓缓", "低声", "忽然", "突然", "猛地", "默默", "静静"):
                dialogue_names.add(name)
        # 策略2：引导词+人名 "青年苏见川" "白发老者"
        for m in re.finditer(
            r"(?:青年|少年|少女|女子|男子|老者|老人|姑娘|公子|书生|将军)"
            r"([\u4e00-\u9fa5]{2,3})", all_text
        ):
            dialogue_names.add(m.group(1))
        # 用全文出现频率筛选（>=2 次才算角色）
        char_names = [n for n in dialogue_names if all_text.count(n) >= 2]
        # 也把通用称谓加进来（如果出现 >=2 次）
        for term in ("老者", "老人", "青年", "少年", "少女", "女子", "男子"):
            if all_text.count(term) >= 2 and term not in char_names:
                char_names.append(term)
        if char_names:
            print(f"  自动检测到角色: {', '.join(char_names)}")

    if not char_names:
        # 仍要 _finalize_prompts（用空 characters 重算，确保双提示词结构对齐）
        _finalize_prompts(sl, cast_map=cast_map or None, props_defs=props_defs or None)
        return

    for shot in sl.shots:
        text = shot.source_text + shot.scene + shot.action + shot.narration
        shot.characters = [name for name in char_names if name in text]

    # 概念模式下角色来自 plan
    if sl.source_type == "concept" and not any(s.characters for s in sl.shots):
        for shot in sl.shots:
            for name in char_names:
                if name in (shot.source_text + shot.narration):
                    shot.characters.append(name)

    # 角色标注完成，用 shot.characters 重新生成 prompt/frame_prompt，
    # 让"视频正文禁角色名 + 分镜图直呼角色名"分工真正落地
    # 同时把 cast_map + props_defs 传给 _finalize_prompts 驱动通用身份合成与道具规范化
    _finalize_prompts(sl, cast_map=cast_map or None, props_defs=props_defs or None)

    # 质量门（同框≤3、台词装得下、构图字段齐全等）
    gate_warnings = validate_shots(sl)
    sl.warnings.extend(gate_warnings)


def write_outputs(sl: ShotList, outdir: str, json_only: bool, pm: ProjectManager = None):
    """写入分镜产物。

    若传入 pm（ProjectManager），产物写入 <project-root>/storyboard/，
    并更新 project.json 的 split 阶段状态；否则写入 outdir 指定目录。
    """
    if pm:
        outdir = pm.get_path("storyboard")
    os.makedirs(outdir, exist_ok=True)
    safe = re.sub(r"[^\w\u4e00-\u9fff-]", "_", sl.title)[:40] or "manju"

    json_path = os.path.join(outdir, f"{safe}.shots.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(sl.to_dict(), f, ensure_ascii=False, indent=2)

    # 更新 project.json（无论 json_only 与否都执行）
    if pm:
        rel_json = os.path.relpath(json_path, pm.root)
        pm.update_stage("split", "completed", output=rel_json)
        pm.set_meta("title", sl.title)
        pm.set_meta("style_key", sl.style_key)
        pm.set_meta("ratio", sl.ratio)

    if json_only:
        print(json_path)
        return json_path

    md = os.path.join(outdir, f"{safe}.storyboard.md")
    txt = os.path.join(outdir, f"{safe}.prompts.txt")
    vo = os.path.join(outdir, f"{safe}.voiceover.md")
    for path, content in (
        (md, render_markdown(sl)),
        (txt, render_prompts_txt(sl)),
        (vo, render_voiceover(sl)),
    ):
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

    print(f"✅ 分镜表    → {md}")
    print(f"✅ 提示词    → {txt}")
    print(f"✅ 配音字幕  → {vo}")
    print(f"✅ 机读 JSON → {json_path}")
    eps = episode_summary(sl.shots)
    print(f"\n共 {len(sl.shots)} 个镜头，总时长 {sum(s.seconds for s in sl.shots)}s，"
          f"单镜最长 {max((s.seconds for s in sl.shots), default=0)}s "
          f"({'符合' if all(s.seconds <= MAX_SHOT_SECONDS for s in sl.shots) else '超标!'} 15s 上限)")
    if eps:
        scope = f"每集 ≤ {sl.episode_shots} 镜" if sl.episode_shots else "不分集"
        print(f"分 {len(eps)} 集（{scope} / "
              f"{'完结章末集收尾' if sl.is_final else '每集结尾留悬念'}）："
              + "，".join(f"第{e['index']}集 镜{e['shot_start']:02d}-{e['shot_end']:02d}"
                          f"({e['shot_count']}镜/{e['seconds']}s·{EPISODE_MARK_SHORT[e['kind']]})"
                          for e in eps))

    return json_path


# ──────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="漫剧分镜拆解引擎（豆包 15 秒单视频约束）")
    ap.add_argument("input", nargs="?", help="小说章节 txt/md 文件路径")
    ap.add_argument("-o", "--out", default="./manju_out", help="输出目录")
    ap.add_argument("-p", "--project-root", default=None,
                    help="项目根目录（启用统一项目管理，产物归档到 storyboard/ 等子目录）")
    ap.add_argument("--title", default=None, help="作品标题（默认取首行或文件名）")
    ap.add_argument("--style", default="3d-realistic", choices=list(STYLE_PRESETS), help="画风预设")
    ap.add_argument("--ratio", default="16:9", choices=list(RATIO_MAP), help="画幅比例")
    ap.add_argument("--model", default=DEFAULT_MODEL, help="豆包视频模型名")
    ap.add_argument("--from-plan", default=None, help="从故事梗概 JSON 拆解（非小说场景）")
    ap.add_argument("--episode-shots", type=int, default=EPISODE_SHOTS,
                    help=f"每集最多几镜（默认 {EPISODE_SHOTS} 镜 ≈ 60 秒；0 = 不分集）")
    ap.add_argument("--is-final", action="store_true",
                    help="本章/本篇为完结章：最后一集正常收尾（默认按连载处理，每集结尾留悬念）")
    ap.add_argument("--characters-json", default=None,
                    help="角色定义 JSON 路径（来自 extract_characters.py 或手写），用于标注每镜出场角色")
    ap.add_argument("--no-continuity", action="store_true",
                    help="跳过衔接字段注入（独立单镜/调试场景）")
    ap.add_argument("--json-only", action="store_true", help="仅输出 JSON")
    args = ap.parse_args()

    # 解析输出目录和项目管理器
    out_dir, pm = resolve_output_dir(args.project_root, args.out)

    # 提前读 characters.json，把 cast_map 传给 from_novel/from_concept
    # 让 build_shot 在初始阶段就能用结构化角色圣经合成通用身份
    cast_map = None
    if args.characters_json:
        try:
            with open(args.characters_json, encoding="utf-8") as f:
                data = json.load(f)
            cast_map = {c["name"]: c for c in data.get("characters", []) if c.get("name")}
        except (FileNotFoundError, json.JSONDecodeError, KeyError) as e:
            print(f"⚠️  读取角色定义失败({e})，cast_map 退化为空")

    if args.from_plan:
        with open(args.from_plan, encoding="utf-8") as f:
            plan = json.load(f)
        sl = from_concept(plan, args.style, args.ratio, args.model,
                          args.episode_shots, args.is_final,
                          cast_map=cast_map, no_continuity=args.no_continuity)
    elif args.input:
        with open(args.input, encoding="utf-8") as f:
            text = f.read()
        title = args.title or guess_title(
            text, os.path.splitext(os.path.basename(args.input))[0]
        )
        sl = from_novel(text, title, args.style, args.ratio, args.model,
                        args.episode_shots, args.is_final,
                        cast_map=cast_map, no_continuity=args.no_continuity)
    else:
        ap.print_help()
        sys.exit(1)

    for w in sl.warnings:
        print(f"⚠️  {w}")

    # 标注每镜出场角色（用于 doubao_gen.py 匹配参考图上传）
    annotate_characters(sl, args.characters_json)

    write_outputs(sl, out_dir, args.json_only, pm=pm)


if __name__ == "__main__":
    main()