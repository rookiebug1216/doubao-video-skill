# -*- coding: utf-8 -*-
"""
画风预设 —— 所有漫剧 skill 共享的视觉风格定义。

确保参考图与最终视频画风统一：同一个 style_key 在 split_novel.py、
char_ref_gen.py、extract_characters.py 中产生完全一致的画风前缀。

风格说明：
  3d-realistic  CG写实3D（默认）：计算机生成仿真人，非真人实拍、非卡通
  3d-cartoon    皮克斯式3D卡通
  guoman-2d     国漫二维赛璐璐
  anime         日式新赛璐璐动漫
  cinematic     写实电影质感（真人实拍观感）
  ink-wash      中国水墨动画写意
"""

STYLE_PRESETS = {
    "3d-realistic": (
        "CG写实3D风格，计算机生成（CGI），UE5电影级实时渲染，3D建模的仿真数字人，"
        "真人比例与真实人体解剖结构，PBR材质，细腻真实的皮肤质感与毛孔细节，"
        "次表面散射，真实毛发与布料模拟，电影级物理光照与全局光照，光线追踪质感，浅景深；"
        "非真人实拍，非卡通，非动漫，非Q版，不要夸张五官与卡通化渲染"
    ),
    "3d-cartoon": (
        "3D动画电影风格，皮克斯式卡通渲染，角色为大眼圆润的卡通造型，"
        "柔和次表面散射光照，电影级景深与体积光，色彩明快饱和，高质量三维建模"
    ),
    "guoman-2d": (
        "国漫二维动画风格，赛璐璐上色，线条清晰凌厉，中国风美术设计，"
        "水墨晕染质感，国漫顶级作画水准"
    ),
    "anime": (
        "日式动漫风格，新赛璐璐作画，线条干净通透，明亮高饱和色彩，"
        "日系动画电影质感，精细背景美术"
    ),
    "cinematic": (
        "写实电影质感，真人实拍风格，电影级布光与调色，浅景深，"
        "35mm胶片颗粒感，高细节皮肤与材质"
    ),
    "ink-wash": (
        "中国水墨动画风格，写意留白，宣纸质感，墨色浓淡变化，"
        "写意山水与人物，动画化水墨笔触"
    ),
}

# 画幅 -> 分辨率提示
RATIO_MAP = {
    "9:16": "竖屏9:16，1080x1920，适合抖音快手短视频",
    "16:9": "横屏16:9，1920x1080，适合B站长视频",
    "1:1": "方形1:1，1080x1080，适合社交媒体信息流",
    "4:3": "经典4:3画幅",
    "21:9": "宽银幕21:9，电影宽画幅",
}

DEFAULT_MODEL = "Seedance 2.0"


def get_style(style_key: str) -> str:
    """获取画风前缀文本，未知 key 回退到 3d-realistic。"""
    return STYLE_PRESETS.get(style_key, STYLE_PRESETS["3d-realistic"])


def get_ratio_desc(ratio: str) -> str:
    """获取画幅描述文本。"""
    return RATIO_MAP.get(ratio, f"自定义画幅 {ratio}")


def list_styles() -> list:
    """列出所有可用画风 key。"""
    return list(STYLE_PRESETS.keys())
