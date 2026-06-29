import json
import re


SOURCE_OPTIONS = [
    "上下游推荐",
    "展会",
    "抖音账号A",
    "抖音账号B",
    "抖音账号C",
    "抖音账号D",
    "抖音账号E",
    "抖音账号F",
    "抖音其他",
    "视频号账号A",
    "视频号账号B",
    "视频号账号C",
    "视频号账号D",
    "视频号账号E",
    "视频号账号F",
    "视频号其他",
    "小红书",
    "快手",
    "TikTok",
    "短视频其他",
    "网站来源",
    "阿里巴巴国际站",
    "ins",
    "脸书",
    "国外社媒其他",
    "外贸",
    "老客户",
    "其他",
]

SOURCE_ALIASES = {
    "上下游": "上下游推荐",
    "老客户介绍": "上下游推荐",
    "客户介绍": "上下游推荐",
    "朋友介绍": "上下游推荐",
    "同行推荐": "上下游推荐",
    "推荐": "上下游推荐",
    "介绍": "上下游推荐",
    "CBCE": "展会",
    "cbce": "展会",
    "展览": "展会",
    "展会客户": "展会",
    "抖音": "抖音其他",
    "抖音号": "抖音其他",
    "抖音号账号A": "抖音账号A",
    "抖音号账号B": "抖音账号B",
    "抖音号账号C": "抖音账号C",
    "抖音号张": "抖音账号F",
    "抖音号账号D": "抖音账号D",
    "抖音号账号E": "抖音账号E",
    "抖音账号A": "抖音账号A",
    "抖音账号B": "抖音账号B",
    "抖音账号C": "抖音账号C",
    "抖音账号D": "抖音账号D",
    "抖音账号E": "抖音账号E",
    "抖音账号F": "抖音账号F",
    "视频号": "视频号其他",
    "视频号账号A": "视频号账号A",
    "视频号账号B": "视频号账号B",
    "视频号账号C": "视频号账号C",
    "视频号账号F": "视频号账号F",
    "视频号账号D": "视频号账号D",
    "视频号账号E": "视频号账号E",
    "短视频": "短视频其他",
    "短视频（停用）": "短视频其他",
    "短视频老线索": "短视频其他",
    "新媒体": "短视频其他",
    "快手": "快手",
    "直播": "短视频其他",
    "小红书": "小红书",
    "小红书 ": "小红书",
    "TikTok": "TikTok",
    "tiktok": "TikTok",
    "官网": "网站来源",
    "网站": "网站来源",
    "独立站": "网站来源",
    "ins": "ins",
    "Ins": "ins",
    "ins ": "ins",
    "instagram": "ins",
    "Instagram": "ins",
    "INSTAGRAM": "ins",
    "照片墙": "ins",
    "fb": "脸书",
    "FB": "脸书",
    "facebook": "脸书",
    "Facebook": "脸书",
    "FACEBOOK": "脸书",
    "脸书": "脸书",
    "meta": "脸书",
    "Meta": "脸书",
    "国外社媒": "国外社媒其他",
    "阿里": "阿里巴巴国际站",
    "阿里巴巴": "阿里巴巴国际站",
    "国际站": "阿里巴巴国际站",
    "账号E分配": "其他",
    "老线索": "其他",
    "账号来源": "",
    "\u3000": "",
}

SOURCE_KEYWORD_RULES = [
    (("推荐", "介绍", "上下游", "配套"), "上下游推荐"),
    (("cbce", "展会", "展览"), "展会"),
    (("官网", "网站", "独立站"), "网站来源"),
    (("阿里", "国际站"), "阿里巴巴国际站"),
    (("外贸",), "外贸"),
    (("老客户",), "老客户"),
]

SHORT_VIDEO_ACCOUNT_RULES = {
    "账号A": "账号A",
    "宋": "账号A",
    "账号B": "账号B",
    "潘": "账号B",
    "账号C": "账号C",
    "销售A": "账号C",
    "账号D": "账号D",
    "账号E": "账号E",
    "张": "张",
}

CUSTOMER_TYPE_OPTIONS = [
    "5000吨以上（大型）",
    "1000-5000吨（中型）",
    "1000吨（小型）",
    "1000吨以下（微型）",
    "精酿啤酒店面",
    "滚轮",
    "封口机",
    "贸易商",
    "上下游配套商",
    "含气饮料",
    "不含气饮料",
]

DEMAND_OPTIONS = [
    "易拉罐单头等压灌装机",
    "易拉罐单头等压封口机",
    "1-1移动式易拉罐灌封一体机",
    "1-1移动式玻璃瓶灌封一体机",
    "1-1移动式马口铁罐灌封一体机",
    "4-1移动式易拉罐灌封一体机",
    "4-1移动式玻璃瓶灌封一体机",
    "4-1移动式马口铁罐灌封一体机",
    "6-1易拉罐灌封一体机",
    "6-1玻璃瓶灌封一体机",
    "6-1马口铁罐灌封一体机",
    "8-2A灌封一体机",
    "8-2B灌封一体机",
    "8-2C灌封一体机",
    "12-2-2灌封一体机",
    "12B灌封一体机",
    "12C灌封一体机",
    "18-3C灌封一体机",
    "18-4-3灌封一体机",
    "18-4B灌封一体机",
    "高速封口机",
    "滚轮",
]

DEMAND_ALIASES = {
    "6-1 易拉罐灌封一体机": "6-1易拉罐灌封一体机",
    "6-1 玻璃瓶灌封一体机": "6-1玻璃瓶灌封一体机",
    "6-1 马口铁罐灌封一体机": "6-1马口铁罐灌封一体机",
}

CUSTOMER_STATUS_OPTIONS = [
    "已加联系方式",
    "未报价",
    "报价中",
    "已报价",
    "待拜访",
    "方案设计沟通中",
    "微信未通过",
    "未加联系方式",
    "已下单",
    "合同已签待预付",
]

CUSTOMER_STATUS_ALIASES = {
    "已加微信": "已加联系方式",
    "已加微信号": "已加联系方式",
    "已加联系方式": "已加联系方式",
    "已加Whatsapp": "已加联系方式",
    "已加WhatsApp": "已加联系方式",
    "已加whatsapp": "已加联系方式",
    "已加WA": "已加联系方式",
    "已加wa": "已加联系方式",
    "未加微信": "未加联系方式",
    "微信未通过": "微信未通过",
    "待到访": "待拜访",
    "已报价": "已报价",
    "报价中": "报价中",
    "未报价": "未报价",
    "待拜访": "待拜访",
    "方案设计沟通中": "方案设计沟通中",
    "已下单": "已下单",
    "合同已签待预付": "合同已签待预付",
}

GRADE_LABEL_TO_CODE = {
    "待孵化": "incubating",
    "待孵化客户": "incubating",
    "潜在": "potential",
    "潜在客户": "potential",
    "一般": "normal",
    "一般客户": "normal",
    "意向": "intention",
    "意向客户": "intention",
    "重点": "key",
    "重点客户": "key",
    "待定": "uncertain",
    "待定客户": "uncertain",
    "无效": "invalid",
    "无效客户": "invalid",
}


def choice_pairs(options, include_blank=True):
    pairs = [(item, item) for item in options]
    return [("", "---------")] + pairs if include_blank else pairs


def parse_multi_value(value):
    if value in ("", None):
        return []
    if isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        text = str(value).strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
            items = parsed if isinstance(parsed, list) else [parsed]
        except json.JSONDecodeError:
            cleaned = re.sub(r"^[\[\"]+|[\]\"]+$", "", text)
            items = re.split(r"\s*(?:,|，|、|;|；)\s*", cleaned)
    result = []
    for item in items:
        text = str(item or "").strip().strip('"').strip("'")
        if text:
            result.append(text)
    return result


def dedupe(items):
    result = []
    seen = set()
    for item in items:
        if item and item not in seen:
            result.append(item)
            seen.add(item)
    return result


def canonical_source(value):
    for item in parse_multi_value(value):
        text = normalize_source_label(item)
        if text:
            return text
    text = str(value or "").strip()
    return normalize_source_label(text)


def normalize_source_label(value):
    text = str(value or "").strip()
    if not text:
        return ""
    aliased = SOURCE_ALIASES.get(text)
    if aliased is not None:
        return aliased
    if text in SOURCE_OPTIONS:
        return text
    compact = re.sub(r"\s+", "", text).lower()
    short_video = _normalize_short_video_source(compact)
    if short_video:
        return short_video
    foreign_social = _normalize_foreign_social_source(compact)
    if foreign_social:
        return foreign_social
    for keywords, target in SOURCE_KEYWORD_RULES:
        if any(keyword.lower() in compact for keyword in keywords):
            return target
    return "其他"


def _normalize_short_video_source(compact):
    if "小红书" in compact:
        return "小红书"
    if "快手" in compact:
        return "快手"
    if "tiktok" in compact:
        return "TikTok"
    for platform, default_target in (("抖音", "抖音其他"), ("视频号", "视频号其他")):
        if platform not in compact:
            continue
        for keyword, account in SHORT_VIDEO_ACCOUNT_RULES.items():
            if keyword.lower() in compact:
                return f"{platform}{account}"
        return default_target
    if any(keyword in compact for keyword in ("短视频", "新媒体", "直播", "巨量")):
        return "短视频其他"
    return ""


def _normalize_foreign_social_source(compact):
    if any(keyword in compact for keyword in ("instagram", "ins", "照片墙")):
        return "ins"
    if any(keyword in compact for keyword in ("facebook", "fb", "脸书", "meta")):
        return "脸书"
    if any(keyword in compact for keyword in ("国外社媒", "海外社媒", "海外媒体", "国外平台")):
        return "国外社媒其他"
    return ""


def canonical_customer_type(value):
    text = str(value or "").strip()
    return text if text in CUSTOMER_TYPE_OPTIONS else text


def canonical_demands(value):
    items = []
    for item in parse_multi_value(value):
        normalized = DEMAND_ALIASES.get(item, re.sub(r"(?<=\d-\d)\s+", "", item).strip())
        if normalized in DEMAND_OPTIONS:
            items.append(normalized)
    return ",".join(dedupe(items))


def canonical_customer_statuses(value):
    items = []
    for item in parse_multi_value(value):
        canonical = CUSTOMER_STATUS_ALIASES.get(item, item)
        if canonical in CUSTOMER_STATUS_OPTIONS:
            items.append(canonical)
    return ",".join(dedupe(items))
