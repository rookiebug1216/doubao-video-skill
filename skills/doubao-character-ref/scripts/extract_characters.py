#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
角色提取器 —— 从小说正文中自动提取主要人物及其外形描述、关键道具。

零第三方依赖：纯 Python 正则 + 频率分析。

产出 characters.json，格式与 doubao-character-ref/scripts/char_ref_gen.py 兼容，
可直接作为 char_ref_gen.py 的输入生成六视图角色参考图。

提取策略：
  1. 人名识别：按"X + 动作动词"模式匹配，频率排序取 top-N
  2. 外形提取：在人名出现的句子中搜索外貌关键词（穿/戴/留/眉/眼/发/体型…）
  3. 道具提取：搜索携带类动词（握/持/佩/悬/带…）+ 物品名词
  4. 合并去重：同一角色的多处描述合并为一段完整外形描述

注意：自动提取是**草稿**，建议人工审校后再生图。AI 对提示词的理解受
描述精度影响极大，模糊词（"帅气""漂亮"）会让每张图画出不同的人。

用法：
  python extract_characters.py chapter.txt -o ./out                    # 提取并输出
  python extract_characters.py chapter.txt -o ./out --top 4            # 只取前4个角色
  python extract_characters.py chapter.txt -o ./out --style 3d-realistic
  python extract_characters.py chapter.txt -o ./out --interactive       # 交互式确认
  python extract_characters.py chapter.txt -o ./out --dry-run           # 只打印不写文件
"""

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict

# ──────────────────────────────────────────────────────────────
# 常量
# ──────────────────────────────────────────────────────────────

# 动作动词——人名通常出现在这些词前面
ACTION_VERBS = re.compile(
    r"(?:说|道|问|答|笑|哭|叹|怒|惊|愣|怔|看|望|瞥|盯|转身|迈步|走出|走进|站|坐|蹲|跪|躺|握|放|抬|挥|拍|抓|扔|接|挡|闪|冲|扑|退|停|低头|抬头|摇头|点头|皱眉)"
)

# 人名引导词——"青年X""老者X""女子X" 等，用于首次出场识别
NAME_INTRO = re.compile(
    r"(?:青年|少年|少女|女子|男子|老者|老人|姑娘|公子|先生|书生|将军|大人|师傅|师父|师叔|师弟|师妹|师兄|师姐|道士|和尚|书童|丫鬟|侍女|护卫|刺客|剑客)"
    r"([\u4e00-\u9fa5]{2,3})"
)

# 代词/常见非人名词——不作为角色名
STOP_WORDS = {
    "他们", "她们", "你们", "我们", "自己", "别人", "众人", "大家", "此人", "对方",
    "什么", "怎么", "为何", "如何", "哪里", "这里", "那里", "这边", "那边",
    "可是", "但是", "不过", "然后", "后来", "终于", "于是", "虽然", "因为",
    "师傅", "师父", "师兄", "师弟", "师姐", "师妹", "老者", "青年", "少年",
    "少女", "女子", "男子", "公子", "先生", "将军", "大人",
    "师门", "江湖", "天下", "世间", "天地", "此刻", "此时", "此地",
    "他低声", "她低声", "他缓缓", "她缓缓", "他猛地", "她猛地",
    "他忽然", "她忽然", "他突然", "她突然",
    "低声", "高声", "缓缓", "猛地", "忽然", "突然", "静静", "默默",
}

# 不能作为人名开头的字（代词/指示词）
_PRONOUN_START = re.compile(r"^[他她它你我妳您这那此某其]")

# 外形关键词——句子含这些词时提取为角色描述
APPEARANCE_KEYWORDS = re.compile(
    r"(?:穿|戴|披|裹|系|套|着|留|蓄|剃|梳|绑|扎|束|"
    r"脸|面|额|颧|下颌|下颚|眉|眼|目|鼻|嘴|唇|耳|肤|"
    r"发|头|鬓|辫|丝|马尾|寸头|长发|短发|卷发|直发|"
    r"身高|身形|身材|体型|修长|壮硕|纤细|纤细|挺拔|魁梧|瘦削|圆润|"
    r"袍|衫|裙|衣|靴|鞋|风衣|毛衣|开衫|外套|长衫|短褐|汉服|和服|"
    r"剑眉|星眸|杏眼|柳眉|鹅蛋脸|方脸|圆脸|尖下巴|高鼻梁|"
    r"疤痕|痣|纹身|刺青|苍白|黝黑|白皙|古铜色|"
    r"岁|年|龄|男|女)"
)

# 道具携带动词
PROP_VERBS = re.compile(
    r"(?:握|持|佩|悬|带|提|怀|执|抱|别|揣|背|扛|拿|捏|捏着|握着|拿着|带着|佩着|挂着|提着|别着|揣着|背着|扛着)"
)

# 常见道具名词
PROP_NOUNS = re.compile(
    r"(?:剑|刀|枪|棍|杖|扇|伞|笛|箫|琴|弓|弩|盾|斧|锤|鞭|锏|"
    r"玉佩|玉|佩|簪|钗|链|珠|符|印|令牌|信|信件|书|卷|册|"
    r"灯笼|灯|烛|香|丹|药|瓶|葫芦|袋|囊|箱|匣|"
    r"面具|斗笠|蓑衣|披风|斗篷)"
)

# 句子分隔符
SENTENCE_SPLIT = re.compile(r"[。！？；\n]+")

# 中文姓名模式：2-4 个连续汉字
CN_NAME_PATTERN = re.compile(r"[\u4e00-\u9fa5]{2,4}")


# ──────────────────────────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────────────────────────

def log(msg: str):
    print(msg, flush=True)


def read_text(filepath: str) -> str:
    """读取文件，自动检测编码。"""
    for enc in ("utf-8", "gbk", "gb2312", "big5", "utf-16"):
        try:
            with open(filepath, encoding=enc) as f:
                return f.read()
        except (UnicodeDecodeError, FileNotFoundError):
            continue
    raise FileNotFoundError(f"无法读取文件: {filepath}")


def split_sentences(text: str) -> list:
    """把正文拆成句子列表。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    parts = SENTENCE_SPLIT.split(text)
    return [p.strip() for p in parts if p.strip() and len(p.strip()) > 3]


# ──────────────────────────────────────────────────────────────
# 角色名识别
# ──────────────────────────────────────────────────────────────

def extract_name_candidates(text: str) -> Counter:
    """
    从正文中识别角色名候选。

    策略（按信号强度排序）：
    1. 对话归属 "X道：" "X说：" —— 最强信号，直接对话归属
    2. 引导词 + 人名 "青年苏见川" —— 首次出场
    3. 句首主语 + 动作动词 "苏见川握着" —— 需过滤代词和副词
    """
    candidates = Counter()

    # 策略1：对话归属 "X道：" "X说：" "X问道，"
    # 非贪婪匹配名 + 可选副词 + 道说
    for m in re.finditer(
        r"([\u4e00-\u9fa5]{2,3}?)(?:缓缓|低声|高声|冷冷|淡淡|轻轻|静静|猛地|忽然|突然|默默)?"
        r"(?:道|说|问|答|笑|喊|骂|叹)(?:[：:，,])", text
    ):
        name = m.group(1)
        if _is_valid_name(name):
            candidates[name] += 3

    # 策略2：引导词 + 人名（贪婪 {2,3}，大多数中文名 2-3 字）
    for m in NAME_INTRO.finditer(text):
        name = m.group(1)
        if _is_valid_name(name):
            candidates[name] += 2

    # 策略3：句首主语 + 动作动词（非贪婪，避免吞掉动词字）
    for m in re.finditer(r"([\u4e00-\u9fa5]{2,3}?)(?=" + ACTION_VERBS.pattern + r")", text):
        name = m.group(1)
        if _is_valid_name(name):
            candidates[name] += 1

    # 策略4：X + 副词 + 道说（"苏见川缓缓道" -> "苏见川"）
    for m in re.finditer(
        r"([\u4e00-\u9fa5]{2,3}?)(?:缓缓|低声|冷冷|淡淡|轻轻|静静)(?:道|说|笑|问)", text
    ):
        name = m.group(1)
        if _is_valid_name(name):
            candidates[name] += 2

    return candidates


def _is_valid_name(name: str) -> bool:
    """判断一个字符串是否可能是角色名（而非代词/副词/常见词）。"""
    if name in STOP_WORDS:
        return False
    # 不以代词开头
    if _PRONOUN_START.match(name):
        return False
    # 不含副词（任何位置）
    _ADVERBS = ("缓缓", "低声", "高声", "冷冷", "淡淡", "轻轻", "静静",
                "猛地", "猛然", "忽然", "突然", "默默", "簌簌")
    for adv in _ADVERBS:
        if adv in name:
            return False
    # 不含虚词/介词/助词（这些字不会出现在人名中）
    if re.search(r"[的了着地将把被对从在于向以为给让]", name):
        return False
    # 不含"声音""立于"等常见组合
    if re.search(r"声音|立于|门内|门外|身后|眼前|身上|手中|心中|眼中|什么|进来|想要", name):
        return False
    # 纯方位/时间词
    if re.match(r"^[上下左右前后内外这里那里]", name):
        return False
    # 纯形容词
    if name in {"英俊", "漂亮", "美丽", "帅气", "高大", "矮小", "清秀", "丑陋"}:
        return False
    return True


def filter_main_characters(candidates: Counter, top_n: int = 5) -> list:
    """
    从候选中筛选主要角色。

    规则：
    - 出现次数 >= 2（短文本也至少出现2次才算角色）
    - 取 top_n 个
    """
    qualified = [(name, cnt) for name, cnt in candidates.items() if cnt >= 2]
    qualified.sort(key=lambda x: -x[1])
    return [name for name, cnt in qualified[:top_n]]


# ──────────────────────────────────────────────────────────────
# 外形描述提取
# ──────────────────────────────────────────────────────────────

# 五类外形字段的关键词模式（用于结构化 persona 拆解）
FACE_RE = re.compile(
    r"(剑眉|星眸|杏眼|柳眉|鹅蛋脸|方脸|圆脸|尖下巴|高鼻梁|"
    r"颧骨(?:分明|突出)?|下颌线(?:硬朗|清晰)?|"
    r"[\u4e00-\u9fff]{0,3}脸|[一-鿿]{0,2}眉|"
    r"[一-鿿]{0,2}眼|[一-鿿]{0,2}鼻|"
    r"疤痕|痣|纹身|刺青|"
    r"苍白|黝黑|白皙|古铜色)"
)
HAIR_RE = re.compile(
    r"(短寸|寸头|短发|长发|卷发|直发|马尾|辫子|"
    r"黑色?发|白色?发|棕色?发|黄色?发|红色?发|"
    r"鬓角(?:剃青|留)?|前额碎发|刘海|"
    r"发[\u4e00-\u9fff]{1,4})"
)
CLOTHING_RE = re.compile(
    r"(穿|披|裹|系|套|着|戴|穿)[\u4e00-\u9fff，、]{2,30}?"
    r"(?:长袍|短袍|风衣|毛衣|开衫|外套|长衫|短褐|汉服|和服|"
    r"裙|衫|衣|靴|鞋|玉佩|玉带|腰带|披风|斗篷|斗笠|蓑衣|"
    r"玉|佩|簪|钗|链|珠|符|印|令牌)"
)
BODY_RE = re.compile(
    r"(修长|挺拔|壮硕|纤细|魁梧|瘦削|圆润|"
    r"身高(?:约|大概)?[\d零一二三四五六七八九十百]{1,4}(?:cm|厘米|公分)?|"
    r"体型[\u4e00-\u9fff]{1,4}|身材[\u4e00-\u9fff]{1,4})"
)


def extract_appearance(sentences: list, char_name: str) -> str:
    """
    从句子中提取角色的外形描述（自由文本，兼容旧版 desc 字段）。

    策略：
    1. 找到包含角色名 + 外形关键词的句子
    2. 从这些句子中提取描述性片段
    3. 合并去重，按维度排序
    """
    desc_parts = []

    for sent in sentences:
        if char_name not in sent:
            continue
        if not APPEARANCE_KEYWORDS.search(sent):
            continue

        # 提取包含关键词的片段
        # 尝试从角色名后面截取描述
        idx = sent.find(char_name)
        if idx >= 0:
            # 取角色名后的部分（通常外形描述在名字之后）
            after = sent[idx + len(char_name):]
            # 如果片段太长，尝试在逗号/顿号处截断
            for sep in ("，", "、", "。"):
                if sep in after:
                    after = after.split(sep)[0]
                    break
            after = after.strip()
            if after and len(after) > 2:
                desc_parts.append(after)

        # 也取角色名前面的部分（可能是"青年苏见川"这种引导）
        before = sent[:idx]
        # 去掉引号
        before = re.sub(r'["""''「]', '', before).strip()
        if before and APPEARANCE_KEYWORDS.search(before):
            # 提取引导词 + 描述
            for m in re.finditer(r"(青年|少年|少女|女子|男子|公子|老者|老人|姑娘|先生)", before):
                desc_parts.append(m.group(1) + char_name)

    # 去重
    seen = set()
    unique = []
    for p in desc_parts:
        if p not in seen and len(p) > 2:
            seen.add(p)
            unique.append(p)

    return "，".join(unique) if unique else ""


def extract_persona(sentences: list, char_name: str,
                    gender: str = "", age: str = "") -> dict:
    """
    结构化 persona 提取：按 face/hair/clothing/body/age/gender/distinguishing/props
    五类字段拆解。

    返回 dict：
      {
        "age": "约30岁", "gender": "男",
        "body": "修长挺拔", "face": "剑眉星眸",
        "hair": "短寸黑发", "clothing": "深灰长风衣",
        "props": ["长剑", "玉佩"],
        "distinguishing": ""
      }
    所有字段命中即填，未命中留空字符串/空列表，由人工审校。
    """
    face_parts, hair_parts, clothing_parts, body_parts = [], [], [], []
    prop_set = set()

    for sent in sentences:
        if char_name not in sent:
            continue
        # 拆 face
        for m in FACE_RE.finditer(sent):
            tok = m.group(0).strip()
            if tok and tok not in face_parts and len(tok) >= 2:
                face_parts.append(tok)
        # 拆 hair
        for m in HAIR_RE.finditer(sent):
            tok = m.group(0).strip()
            if tok and tok not in hair_parts and len(tok) >= 2:
                hair_parts.append(tok)
        # 拆 clothing
        for m in CLOTHING_RE.finditer(sent):
            tok = m.group(0).strip()
            if tok and tok not in clothing_parts and len(tok) >= 3:
                clothing_parts.append(tok)
        # 拆 body
        for m in BODY_RE.finditer(sent):
            tok = m.group(0).strip()
            if tok and tok not in body_parts and len(tok) >= 2:
                body_parts.append(tok)
        # 拆 props
        for m in PROP_NOUNS.finditer(sent):
            tok = m.group(0)
            # 确认是携带物（前面的动词是携带类）
            start = max(0, m.start() - 6)
            ctx = sent[start:m.start()]
            if PROP_VERBS.search(ctx):
                if tok not in prop_set and tok not in ("玉", "佩"):  # 单字道具过滤
                    prop_set.add(tok)

    return {
        "age": age or "",
        "gender": (gender or "").replace("性", ""),  # "男性" → "男"
        "body": "，".join(body_parts[:3]) if body_parts else "",
        "face": "，".join(face_parts[:4]) if face_parts else "",
        "hair": "，".join(hair_parts[:3]) if hair_parts else "",
        "clothing": "，".join(clothing_parts[:4]) if clothing_parts else "",
        "props": list(prop_set)[:3],
        "distinguishing": "",  # 识别特征需要人工审校，自动提取容易误判
    }


def build_generic_identity(name: str, persona: dict) -> dict:
    """
    根据结构化 persona 合成 generic_identity 初稿。

    返回：
      {
        "video_term": "深灰长风衣的持剑青年",   # 禁角色名，投豆包视频正文
        "frame_term": "苏见川（约30岁，剑眉星眸，深灰长风衣）"  # 含角色名，投分镜图
      }

    人类审校后再生图：自动合成仅是初稿，可能漏掉关键特征。
    """
    age = persona.get("age", "")
    gender = (persona.get("gender", "") or "").strip()
    clothing = persona.get("clothing", "")
    face = persona.get("face", "")
    hair = persona.get("hair", "")
    props = persona.get("props", [])

    # 性别词
    if gender.startswith("男"):
        gender_term = "青年" if age and any(x in age for x in ("20", "25", "30")) else "男子"
    elif gender.startswith("女"):
        gender_term = "女子"
    else:
        gender_term = "人物"

    # video_term：禁角色名，颜色+服饰+道具+性别词
    v_parts = []
    if clothing:
        v_parts.append(clothing[:8])  # 取前 8 字
    if props:
        v_parts.append(f"持{props[0]}")
    if gender_term:
        v_parts.append(gender_term)
    video_term = "".join(v_parts) if v_parts else name

    # frame_term：含角色名 + 关键特征
    info = []
    if age:
        info.append(age)
    if face:
        info.append(face)
    if clothing:
        info.append(clothing)
    if hair:
        info.append(hair)
    frame_term = f"{name}（{', '.join(info[:3])}）" if info else name

    return {"video_term": video_term, "frame_term": frame_term}


def extract_aliases(text: str, char_name: str) -> list:
    """
    提取角色别名（同一角色的多个称呼）。

    策略：检测"X也被称为Y""X，又称Y""X，外号Y"等模式；
    以及"小X""老X""阿X"等昵称前缀（仅当中文姓氏 ≥2 字时）。
    """
    aliases = []
    # 模式1：显式别名声明
    for m in re.finditer(
        rf"{char_name}[^。！？]{{0,10}}?(?:也被称为|又称|外号|绰号|别号|又名|也叫)[\u4e00-\u9fff]{{2,4}}",
        text
    ):
        # 提取别名（最后一个汉字串）
        m2 = re.search(r"[\u4e00-\u9fff]{2,4}$", m.group(0))
        if m2 and m2.group(0) != char_name:
            aliases.append(m2.group(0))

    # 模式2：姓氏 + 称呼（如"苏剑客""林姑娘"）
    if len(char_name) >= 2:
        surname = char_name[0]
        for pat in (rf"{surname}(?:剑客|少侠|大侠|公子|姑娘|小姐|先生|前辈|老|小)"):
            for m in re.finditer(pat, text):
                if m.group(0) != char_name and m.group(0) not in aliases:
                    aliases.append(m.group(0))

    return list(dict.fromkeys(aliases))[:3]  # 去重保序，最多 3 个


def extract_gender_age(sentences: list, char_name: str) -> tuple:
    """
    推测角色的性别和年龄段。
    返回 (gender, age_hint)。
    """
    gender = None
    age_hint = None

    for sent in sentences:
        if char_name not in sent:
            continue

        # 性别
        if not gender:
            if re.search(r"(他|男子|公子|先生|青年|少年|男人|父|兄|爷)", sent):
                gender = "男性"
            elif re.search(r"(她|女子|姑娘|少女|女人|母|姐|妹|婆)", sent):
                gender = "女性"

        # 年龄
        if not age_hint:
            age_m = re.search(r"(\d{1,3})\s*岁", sent)
            if age_m:
                age_hint = age_m.group(1) + "岁"
            elif re.search(r"青年|少年", sent):
                age_hint = "青年"
            elif re.search(r"老者|老人|白发|苍老|花甲|古稀", sent):
                age_hint = "老年"
            elif re.search(r"少女|姑娘", sent):
                age_hint = "年轻"

    return gender, age_hint


# ──────────────────────────────────────────────────────────────
# 道具提取
# ──────────────────────────────────────────────────────────────

def extract_props(sentences: list, char_names: list) -> list:
    """
    从句子中提取道具。
    返回 [{"name": "...", "desc": "..."}] 列表。
    """
    props = []
    seen_props = set()

    for sent in sentences:
        # 搜索携带动词 + 道具名词的组合
        for m in PROP_VERBS.finditer(sent):
            # 取动词后的 2-6 字
            start = m.end()
            after = sent[start:start + 10]

            # 在后续文本中找道具名词
            prop_match = PROP_NOUNS.search(after)
            if prop_match:
                prop_name = prop_match.group(0)
                if prop_name in seen_props:
                    continue
                seen_props.add(prop_name)

                # 取完整描述（动词 + 名词 + 后续修饰）
                end = start + prop_match.end()
                desc_start = max(0, m.start())
                desc = sent[desc_start:end + 5].strip()
                # 清理
                desc = re.sub(r"[，。！？；\n]", "", desc)

                # 关联到角色
                owner = None
                for cn in char_names:
                    if cn in sent:
                        owner = cn
                        break

                props.append({
                    "name": prop_name,
                    "desc": desc if len(desc) > len(prop_name) else prop_name + "，重要道具",
                    "owner": owner,
                })

    # 去重：同名道具只保留描述最丰富的
    unique = {}
    for p in props:
        name = p["name"]
        if name not in unique or len(p["desc"]) > len(unique[name]["desc"]):
            unique[name] = p

    return list(unique.values())[:5]  # 最多5个道具


# ──────────────────────────────────────────────────────────────
# 主提取流程
# ──────────────────────────────────────────────────────────────

def extract_characters(text: str, top_n: int = 5, style: str = "3d-realistic",
                       project_name: str = None) -> dict:
    """
    从小说正文中提取角色和道具，返回 characters.json 格式的 dict。
    """
    sentences = split_sentences(text)

    # 0. 推断项目名（取标题行）
    if not project_name:
        title_m = re.search(r"第[一二三四五六七八九十百零\d]+章\s*(.+)", text)
        if title_m:
            project_name = title_m.group(1).strip()
        else:
            project_name = "未命名项目"

    # 1. 识别角色名
    candidates = extract_name_candidates(text)
    char_names = filter_main_characters(candidates, top_n=top_n)

    # 1b. 如果角色太少，尝试从对话归属中提取通用称谓（老者/青年/女子等）
    if len(char_names) < 2:
        generic = extract_generic_characters(text, candidates)
        for g in generic:
            if g not in char_names and len(char_names) < top_n:
                char_names.append(g)
                candidates[g] = candidates.get(g, 2)

    if not char_names:
        return {
            "project": project_name,
            "style": style,
            "characters": [],
            "props": [],
            "_warning": "未能从正文中识别出角色。请手动编写 characters.json。",
            "_candidates": dict(candidates.most_common(10)),
        }

    log(f"\n识别到 {len(char_names)} 个角色候选: {', '.join(char_names)}")
    log(f"（频率: {', '.join(f'{n}={candidates[n]}' for n in char_names)}）")

    # 2. 提取每个角色的外形描述（结构化 persona + aliases + generic_identity）
    characters = []
    for idx, name in enumerate(char_names):
        gender, age_hint = extract_gender_age(sentences, name)
        appearance = extract_appearance(sentences, name)

        # 结构化 persona
        persona = extract_persona(sentences, name, gender=gender or "", age=age_hint or "")

        # 合成完整描述（兼容旧版 desc 字段）
        parts = []
        if age_hint:
            parts.append(age_hint)
        if gender:
            parts.append(gender)
        if appearance:
            parts.append(appearance)
        desc = "，".join(parts) if parts else f"（自动提取未获得外形描述，请手动补充：年龄、性别、面部特征、发型发色、服装、体型）"

        # 别名
        aliases = extract_aliases(text, name)

        # 通用身份初稿
        generic_identity = build_generic_identity(name, persona)

        # importance：按频率粗判
        freq = candidates[name]
        if idx == 0 and freq >= 5:
            importance = "protagonist"
        elif freq >= 3:
            importance = "major"
        else:
            importance = "minor"

        characters.append({
            "name": name,
            "aliases": aliases,
            "importance": importance,
            "desc": desc,
            "persona": persona,
            "generic_identity": generic_identity,
            "_frequency": freq,
            "_auto_extracted": True,
        })

    # 3. 提取道具
    props = extract_props(sentences, char_names)

    return {
        "project": project_name,
        "style": style,
        "characters": characters,
        "props": props,
        "_stats": {
            "total_sentences": len(sentences),
            "name_candidates": len(candidates),
            "characters_found": len(characters),
            "props_found": len(props),
        },
    }


def extract_generic_characters(text: str, candidates: Counter) -> list:
    """
    从对话归属中提取通用称谓（老者/青年/女子等）。
    这些词在 STOP_WORDS 中，但如果出现在"X道：""X说："模式中，说明是角色。
    """
    generic_terms = [
        "老者", "老人", "青年", "少年", "少女", "女子", "男子",
        "姑娘", "公子", "书生", "将军",
    ]
    found = []
    for term in generic_terms:
        # 检查是否出现在对话归属中（允许中间有副词："老者缓缓道"）
        if re.search(term + r"(?:缓缓|低声|高声|冷冷|淡淡|轻轻|静静|猛地|忽然|突然|默默)?"
                     r"(?:道|说|问|答|笑|喊|骂|叹)(?:[：:，,])", text):
            count = len(re.findall(term, text))
            if count >= 2:
                found.append(term)
                candidates[term] = candidates.get(term, 0) + count
    return found


# ──────────────────────────────────────────────────────────────
# 交互式确认
# ──────────────────────────────────────────────────────────────

def interactive_review(data: dict) -> dict:
    """
    交互式审校：让用户确认/编辑每个角色。
    """
    log("\n" + "=" * 50)
    log("角色提取结果审校")
    log("=" * 50)

    for i, char in enumerate(data["characters"]):
        log(f"\n角色 {i + 1}: {char['name']}")
        log(f"  描述: {char['desc']}")
        log(f"  (频率: {char.get('_frequency', '?')})")

        confirm = input("\n  保留此角色？(y/n/e=编辑): ").strip().lower()
        if confirm == "n":
            char["_skip"] = True
        elif confirm == "e":
            new_desc = input("  输入新描述: ").strip()
            if new_desc:
                char["desc"] = new_desc

    # 过滤掉标记跳过的
    data["characters"] = [c for c in data["characters"] if not c.get("_skip")]

    # 道具审校
    if data.get("props"):
        log(f"\n道具列表:")
        for i, prop in enumerate(data["props"]):
            log(f"  {i + 1}. {prop['name']}: {prop['desc']}")
        confirm = input("\n保留所有道具？(y/n): ").strip().lower()
        if confirm == "n":
            keep = input("  输入要保留的道具编号(逗号分隔): ").strip()
            keep_ids = {int(x) for x in keep.split(",") if x.strip()}
            data["props"] = [p for i, p in enumerate(data["props"]) if i + 1 in keep_ids]

    return data


# ──────────────────────────────────────────────────────────────
# 主函数
# ──────────────────────────────────────────────────────────────

def main():
    if os.name == "nt":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    # 加载共享画风预设
    _HERE = os.path.dirname(os.path.abspath(__file__))
    _ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)
    from shared import style_presets

    ap = argparse.ArgumentParser(
        description="角色提取器：从小说正文中自动提取主要人物与道具")
    ap.add_argument("input", help="小说章节文件（txt/md）")
    ap.add_argument("-o", "--out", default="./out", help="输出目录")
    ap.add_argument("--top", type=int, default=5,
                    help="最多提取多少个角色（默认5，对应豆包每日免费额度）")
    ap.add_argument("--style", default="3d-realistic",
                    choices=style_presets.list_styles(),
                    help="画风预设（默认 3d-realistic）")
    ap.add_argument("--project", default=None, help="项目名（默认从章节标题推断）")
    ap.add_argument("--interactive", action="store_true",
                    help="交互式审校每个角色")
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印结果不写文件")
    args = ap.parse_args()

    # 读取小说
    text = read_text(args.input)

    # 先推断项目名用于显示
    display_name = args.project
    if not display_name:
        title_m = re.search(r"第[一二三四五六七八九十百零\d]+章\s*(.+)", text)
        if title_m:
            display_name = title_m.group(1).strip()
        else:
            display_name = "未命名"

    log("=" * 50)
    log(f"《{display_name}》角色提取")
    log(f"画风: {args.style} | 最多 {args.top} 个角色")
    log("=" * 50)

    # 提取
    data = extract_characters(text, top_n=args.top, style=args.style,
                              project_name=args.project)

    # 交互式审校
    if args.interactive:
        data = interactive_review(data)

    # 清理内部字段
    for c in data.get("characters", []):
        c.pop("_frequency", None)
        c.pop("_auto_extracted", None)

    # 输出
    log(f"\n{'=' * 50}")
    log(f"提取完成:")
    log(f"  角色: {len(data.get('characters', []))} 个")
    log(f"  道具: {len(data.get('props', []))} 个")
    if data.get("_stats"):
        log(f"  统计: {data['_stats']}")
    if data.get("_warning"):
        log(f"  ⚠ {data['_warning']}")

    for c in data.get("characters", []):
        log(f"\n  [{c['name']}]")
        log(f"    {c['desc'][:100]}{'…' if len(c['desc']) > 100 else ''}")

    for p in data.get("props", []):
        log(f"\n  道具: [{p['name']}] {p.get('desc', '')[:80]}")

    if args.dry_run:
        log("\n(dry-run) 未写文件。去掉 --dry-run 即开始写入。")
        return

    # 写文件
    os.makedirs(args.out, exist_ok=True)
    # 文件名用项目名
    safe_name = re.sub(r'[^\w\u4e00-\u9fa5]', '_', data.get("project", "characters"))
    outpath = os.path.join(args.out, f"{safe_name}.characters.json")
    with open(outpath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    log(f"\n已写入: {outpath}")
    log(f"下一步: python skills/doubao-character-ref/scripts/char_ref_gen.py "
        f"{outpath} -o ./char_refs")
    log("=" * 50)


if __name__ == "__main__":
    main()
