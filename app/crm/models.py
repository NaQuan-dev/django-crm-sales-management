import hashlib
import re
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.files.storage import default_storage
from django.db import models
from django.utils import timezone


PHONE_SPLIT_RE = re.compile(r"\s*(?:/|,|，|、|;|；)\s*")
CHINA_MOBILE_RE = re.compile(r"^1[3-9]\d{9}$")
NANP_LOCAL_RE = re.compile(r"^[2-9]\d{9}$")
NANP_WITH_CODE_RE = re.compile(r"^1[2-9]\d{9}$")
PLACEHOLDER_USER_NAMES = {"未分配", "无", "暂无", "-", "--"}
OWNER_CODE_NAME_ALIASES = {
    "SALES001": "销售A",
    "SALES002": "销售B",
    "SALES003": "销售C",
    "SALES004": "销售D",
}
OWNER_NAME_ALIASES = {
    "销售D旧名": "销售D",
}
CUSTOMER_NO_PREFIX = "NQKH"
CUSTOMER_NO_WIDTH = 6
CUSTOMER_NO_RE = re.compile(rf"^{CUSTOMER_NO_PREFIX}\d+$")


def next_monthly_no(model, field_name, prefix, width=3):
    stamp = timezone.localtime(timezone.now()).strftime("%Y%m")
    full_prefix = f"{prefix}{stamp}"
    max_number = 0
    for value in model.objects.filter(**{f"{field_name}__startswith": full_prefix}).values_list(field_name, flat=True):
        text = str(value or "").strip()
        suffix = text[len(full_prefix):]
        if suffix.isdigit():
            max_number = max(max_number, int(suffix))
    return f"{full_prefix}{max_number + 1:0{width}d}"


def is_system_customer_no(value):
    return bool(CUSTOMER_NO_RE.match(str(value or "").strip()))


def _dedupe_phone_prefix_choices(*groups):
    result = []
    seen = set()
    for group in groups:
        for prefix, label in group:
            if prefix in seen:
                continue
            seen.add(prefix)
            result.append((prefix, label))
    return result


COMMON_PHONE_PREFIX_CHOICES = [
    ("+86", "+86 中国"),
    ("+1", "+1 美国/加拿大/安圭拉/安提瓜和巴布达/巴哈马/巴巴多斯/百慕大/英属维尔京群岛/开曼群岛/多米尼克/多米尼加/格林纳达/牙买加/蒙特塞拉特/圣基茨和尼维斯/圣卢西亚/圣文森特和格林纳丁斯/特立尼达和多巴哥/特克斯和凯科斯/美属维尔京群岛"),
    ("+44", "+44 英国/根西岛/马恩岛/泽西岛"),
    ("+61", "+61 澳大利亚/圣诞岛/科科斯群岛"),
    ("+65", "+65 新加坡"),
    ("+60", "+60 马来西亚"),
    ("+66", "+66 泰国"),
    ("+84", "+84 越南"),
    ("+81", "+81 日本"),
    ("+82", "+82 韩国"),
    ("+91", "+91 印度"),
    ("+49", "+49 德国"),
    ("+33", "+33 法国"),
    ("+39", "+39 意大利"),
    ("+34", "+34 西班牙"),
    ("+31", "+31 荷兰"),
    ("+7", "+7 俄罗斯/哈萨克斯坦"),
    ("+55", "+55 巴西"),
    ("+52", "+52 墨西哥"),
    ("+971", "+971 阿联酋"),
    ("+852", "+852 香港"),
    ("+853", "+853 澳门"),
    ("+886", "+886 台湾"),
    ("+62", "+62 印尼"),
    ("+63", "+63 菲律宾"),
    ("+92", "+92 巴基斯坦"),
    ("+880", "+880 孟加拉"),
    ("+90", "+90 土耳其"),
    ("+27", "+27 南非"),
    ("+20", "+20 埃及"),
]
GLOBAL_PHONE_PREFIX_CHOICES = [
    ("+1", "+1 美国/加拿大/安圭拉/安提瓜和巴布达/巴哈马/巴巴多斯/百慕大/英属维尔京群岛/开曼群岛/多米尼克/多米尼加/格林纳达/牙买加/蒙特塞拉特/圣基茨和尼维斯/圣卢西亚/圣文森特和格林纳丁斯/特立尼达和多巴哥/特克斯和凯科斯/美属维尔京群岛"),
    ("+7", "+7 俄罗斯/哈萨克斯坦"),
    ("+20", "+20 埃及"),
    ("+27", "+27 南非"),
    ("+30", "+30 希腊"),
    ("+31", "+31 荷兰"),
    ("+32", "+32 比利时"),
    ("+33", "+33 法国"),
    ("+34", "+34 西班牙"),
    ("+36", "+36 匈牙利"),
    ("+39", "+39 意大利/梵蒂冈"),
    ("+40", "+40 罗马尼亚"),
    ("+41", "+41 瑞士"),
    ("+43", "+43 奥地利"),
    ("+44", "+44 英国/根西岛/马恩岛/泽西岛"),
    ("+45", "+45 丹麦"),
    ("+46", "+46 瑞典"),
    ("+47", "+47 挪威/斯瓦尔巴和扬马延"),
    ("+48", "+48 波兰"),
    ("+49", "+49 德国"),
    ("+51", "+51 秘鲁"),
    ("+52", "+52 墨西哥"),
    ("+53", "+53 古巴"),
    ("+54", "+54 阿根廷"),
    ("+55", "+55 巴西"),
    ("+56", "+56 智利"),
    ("+57", "+57 哥伦比亚"),
    ("+58", "+58 委内瑞拉"),
    ("+60", "+60 马来西亚"),
    ("+61", "+61 澳大利亚/圣诞岛/科科斯群岛"),
    ("+62", "+62 印度尼西亚"),
    ("+63", "+63 菲律宾"),
    ("+64", "+64 新西兰/皮特凯恩群岛"),
    ("+65", "+65 新加坡"),
    ("+66", "+66 泰国"),
    ("+81", "+81 日本"),
    ("+82", "+82 韩国"),
    ("+84", "+84 越南"),
    ("+86", "+86 中国"),
    ("+90", "+90 土耳其"),
    ("+91", "+91 印度"),
    ("+92", "+92 巴基斯坦"),
    ("+93", "+93 阿富汗"),
    ("+94", "+94 斯里兰卡"),
    ("+95", "+95 缅甸"),
    ("+98", "+98 伊朗"),
    ("+211", "+211 南苏丹"),
    ("+212", "+212 摩洛哥/西撒哈拉"),
    ("+213", "+213 阿尔及利亚"),
    ("+216", "+216 突尼斯"),
    ("+218", "+218 利比亚"),
    ("+220", "+220 冈比亚"),
    ("+221", "+221 塞内加尔"),
    ("+222", "+222 毛里塔尼亚"),
    ("+223", "+223 马里"),
    ("+224", "+224 几内亚"),
    ("+225", "+225 科特迪瓦"),
    ("+226", "+226 布基纳法索"),
    ("+227", "+227 尼日尔"),
    ("+228", "+228 多哥"),
    ("+229", "+229 贝宁"),
    ("+230", "+230 毛里求斯"),
    ("+231", "+231 利比里亚"),
    ("+232", "+232 塞拉利昂"),
    ("+233", "+233 加纳"),
    ("+234", "+234 尼日利亚"),
    ("+235", "+235 乍得"),
    ("+236", "+236 中非"),
    ("+237", "+237 喀麦隆"),
    ("+238", "+238 佛得角"),
    ("+239", "+239 圣多美和普林西比"),
    ("+240", "+240 赤道几内亚"),
    ("+241", "+241 加蓬"),
    ("+242", "+242 刚果共和国"),
    ("+243", "+243 刚果民主共和国"),
    ("+244", "+244 安哥拉"),
    ("+245", "+245 几内亚比绍"),
    ("+246", "+246 英属印度洋领地"),
    ("+247", "+247 阿森松岛"),
    ("+248", "+248 塞舌尔"),
    ("+249", "+249 苏丹"),
    ("+250", "+250 卢旺达"),
    ("+251", "+251 埃塞俄比亚"),
    ("+252", "+252 索马里"),
    ("+253", "+253 吉布提"),
    ("+254", "+254 肯尼亚"),
    ("+255", "+255 坦桑尼亚"),
    ("+256", "+256 乌干达"),
    ("+257", "+257 布隆迪"),
    ("+258", "+258 莫桑比克"),
    ("+260", "+260 赞比亚"),
    ("+261", "+261 马达加斯加"),
    ("+262", "+262 留尼汪/马约特"),
    ("+263", "+263 津巴布韦"),
    ("+264", "+264 纳米比亚"),
    ("+265", "+265 马拉维"),
    ("+266", "+266 莱索托"),
    ("+267", "+267 博茨瓦纳"),
    ("+268", "+268 斯威士兰"),
    ("+269", "+269 科摩罗"),
    ("+290", "+290 圣赫勒拿/特里斯坦-达库尼亚"),
    ("+291", "+291 厄立特里亚"),
    ("+297", "+297 阿鲁巴"),
    ("+298", "+298 法罗群岛"),
    ("+299", "+299 格陵兰"),
    ("+350", "+350 直布罗陀"),
    ("+351", "+351 葡萄牙"),
    ("+352", "+352 卢森堡"),
    ("+353", "+353 爱尔兰"),
    ("+354", "+354 冰岛"),
    ("+355", "+355 阿尔巴尼亚"),
    ("+356", "+356 马耳他"),
    ("+357", "+357 塞浦路斯"),
    ("+358", "+358 芬兰/奥兰群岛"),
    ("+359", "+359 保加利亚"),
    ("+370", "+370 立陶宛"),
    ("+371", "+371 拉脱维亚"),
    ("+372", "+372 爱沙尼亚"),
    ("+373", "+373 摩尔多瓦"),
    ("+374", "+374 亚美尼亚"),
    ("+375", "+375 白俄罗斯"),
    ("+376", "+376 安道尔"),
    ("+377", "+377 摩纳哥"),
    ("+378", "+378 圣马力诺"),
    ("+380", "+380 乌克兰"),
    ("+381", "+381 塞尔维亚"),
    ("+382", "+382 黑山"),
    ("+383", "+383 科索沃"),
    ("+385", "+385 克罗地亚"),
    ("+386", "+386 斯洛文尼亚"),
    ("+387", "+387 波黑"),
    ("+389", "+389 北马其顿"),
    ("+420", "+420 捷克"),
    ("+421", "+421 斯洛伐克"),
    ("+423", "+423 列支敦士登"),
    ("+500", "+500 福克兰群岛/南乔治亚和南桑威奇群岛"),
    ("+501", "+501 伯利兹"),
    ("+502", "+502 危地马拉"),
    ("+503", "+503 萨尔瓦多"),
    ("+504", "+504 洪都拉斯"),
    ("+505", "+505 尼加拉瓜"),
    ("+506", "+506 哥斯达黎加"),
    ("+507", "+507 巴拿马"),
    ("+508", "+508 圣皮埃尔和密克隆"),
    ("+509", "+509 海地"),
    ("+590", "+590 瓜德罗普/圣巴泰勒米/法属圣马丁"),
    ("+591", "+591 玻利维亚"),
    ("+592", "+592 圭亚那"),
    ("+593", "+593 厄瓜多尔"),
    ("+594", "+594 法属圭亚那"),
    ("+595", "+595 巴拉圭"),
    ("+596", "+596 马提尼克"),
    ("+597", "+597 苏里南"),
    ("+598", "+598 乌拉圭"),
    ("+599", "+599 库拉索/荷兰加勒比区"),
    ("+670", "+670 东帝汶"),
    ("+672", "+672 澳大利亚外岛/诺福克岛"),
    ("+673", "+673 文莱"),
    ("+674", "+674 瑙鲁"),
    ("+675", "+675 巴布亚新几内亚"),
    ("+676", "+676 汤加"),
    ("+677", "+677 所罗门群岛"),
    ("+678", "+678 瓦努阿图"),
    ("+679", "+679 斐济"),
    ("+680", "+680 帕劳"),
    ("+681", "+681 瓦利斯和富图纳"),
    ("+682", "+682 库克群岛"),
    ("+683", "+683 纽埃"),
    ("+685", "+685 萨摩亚"),
    ("+686", "+686 基里巴斯"),
    ("+687", "+687 新喀里多尼亚"),
    ("+688", "+688 图瓦卢"),
    ("+689", "+689 法属波利尼西亚"),
    ("+690", "+690 托克劳"),
    ("+691", "+691 密克罗尼西亚"),
    ("+692", "+692 马绍尔群岛"),
    ("+850", "+850 朝鲜"),
    ("+852", "+852 香港"),
    ("+853", "+853 澳门"),
    ("+855", "+855 柬埔寨"),
    ("+856", "+856 老挝"),
    ("+880", "+880 孟加拉"),
    ("+886", "+886 台湾"),
    ("+960", "+960 马尔代夫"),
    ("+961", "+961 黎巴嫩"),
    ("+962", "+962 约旦"),
    ("+963", "+963 叙利亚"),
    ("+964", "+964 伊拉克"),
    ("+965", "+965 科威特"),
    ("+966", "+966 沙特阿拉伯"),
    ("+967", "+967 也门"),
    ("+968", "+968 阿曼"),
    ("+970", "+970 巴勒斯坦"),
    ("+971", "+971 阿联酋"),
    ("+972", "+972 以色列"),
    ("+973", "+973 巴林"),
    ("+974", "+974 卡塔尔"),
    ("+975", "+975 不丹"),
    ("+976", "+976 蒙古"),
    ("+977", "+977 尼泊尔"),
    ("+992", "+992 塔吉克斯坦"),
    ("+993", "+993 土库曼斯坦"),
    ("+994", "+994 阿塞拜疆"),
    ("+995", "+995 格鲁吉亚"),
    ("+996", "+996 吉尔吉斯斯坦"),
    ("+998", "+998 乌兹别克斯坦"),
]
PHONE_PREFIX_CHOICES = _dedupe_phone_prefix_choices(COMMON_PHONE_PREFIX_CHOICES, GLOBAL_PHONE_PREFIX_CHOICES)
PHONE_PREFIX_VALUES = [prefix for prefix, _label in PHONE_PREFIX_CHOICES]
WECHAT_MARKER_RE = re.compile(
    r"(?:^|[\s,，;；/、])(?:(?:v|vx|wx|wechat|weixin)(?![A-Za-z0-9_.-])|微信号?|微.?信)\s*[:：=]?\s*([A-Za-z0-9][A-Za-z0-9_.-]{1,63})",
    re.IGNORECASE,
)
WECHAT_TOKEN_RE = re.compile(r"(?<!@)\b[A-Za-z0-9][A-Za-z0-9_.-]{1,63}\b(?!@)")
PHONE_MARKER_RE = re.compile(
    r"(?:^|[\s,，;；/、])(?:电话|手机|手机号|客户电话|联系电话|tel|phone|mobile|whatsapp|wa)\s*[:：=]?\s*((?:\+|＋|00)?\d[\d\s\-()（）]{5,}\d)",
    re.IGNORECASE,
)
PHONE_IN_TEXT_RE = re.compile(r"(?<![A-Za-z0-9])(?:\+|＋|00)?\d[\d\s\-()（）]{5,}\d(?![A-Za-z0-9])")
REGION_PAREN_SUFFIX_RE = re.compile(r"^(?P<name>.+?)[（(](?P<region>[\u4e00-\u9fff]{2,12})[）)]$")
REGION_SEPARATOR_SUFFIX_RE = re.compile(r"^(?P<name>.+?)[\s\-—–_]+(?P<region>[\u4e00-\u9fff]{2,12})$")
NON_WECHAT_TOKENS = {
    "v",
    "vx",
    "wx",
    "wechat",
    "weixin",
    "phone",
    "mobile",
    "tel",
    "telephone",
    "whatsapp",
}
NON_REGION_SUFFIX_WORDS = {
    "公司",
    "客户",
    "联系人",
    "先生",
    "女士",
    "老板",
    "经理",
    "设备",
    "机器",
    "灌装",
    "封口",
    "啤酒",
    "饮料",
    "酒厂",
    "工厂",
    "门店",
    "项目",
}
PHONE_PREFIX_BY_HINT = [
    ("+1", ["美国", "加拿大", "usa", "u.s.", "united states", "canada", "atlanta", "new york", "los angeles"]),
    ("+44", ["英国", "uk", "united kingdom", "england"]),
    ("+61", ["澳大利亚", "澳洲", "australia"]),
    ("+65", ["新加坡", "singapore"]),
    ("+60", ["马来西亚", "malaysia"]),
    ("+66", ["泰国", "thailand"]),
    ("+84", ["越南", "vietnam"]),
    ("+81", ["日本", "japan"]),
    ("+82", ["韩国", "korea"]),
    ("+91", ["印度", "india"]),
    ("+49", ["德国", "germany"]),
    ("+33", ["法国", "france"]),
    ("+39", ["意大利", "italy"]),
    ("+34", ["西班牙", "spain"]),
    ("+31", ["荷兰", "netherlands", "holland"]),
    ("+7", ["俄罗斯", "russia"]),
    ("+55", ["巴西", "brazil"]),
    ("+52", ["墨西哥", "mexico"]),
    ("+971", ["阿联酋", "迪拜", "dubai", "uae"]),
    ("+852", ["香港", "hong kong"]),
    ("+853", ["澳门", "macau"]),
    ("+886", ["台湾", "taiwan"]),
    ("+62", ["印尼", "印度尼西亚", "indonesia"]),
    ("+63", ["菲律宾", "philippines"]),
    ("+92", ["巴基斯坦", "pakistan"]),
    ("+880", ["孟加拉", "bangladesh"]),
    ("+90", ["土耳其", "turkey"]),
    ("+27", ["南非", "south africa"]),
    ("+20", ["埃及", "egypt"]),
    ("+86", ["中国", "国内", "china", "大陆"]),
]


def _phone_prefix_label_keywords(label):
    text = re.sub(r"^\+\d+\s*", "", str(label or "")).lower()
    keywords = []
    for part in re.split(r"[/、,，()（）\s]+", text):
        keyword = part.strip()
        if len(keyword) >= 2:
            keywords.append(keyword)
    return keywords


def infer_phone_prefix(*hints):
    text = " ".join(str(hint or "") for hint in hints).lower()
    for prefix, keywords in PHONE_PREFIX_BY_HINT:
        if any(keyword.lower() in text for keyword in keywords):
            return prefix
    for prefix, label in PHONE_PREFIX_CHOICES:
        if any(keyword in text for keyword in _phone_prefix_label_keywords(label)):
            return prefix
    return ""


def split_phone_prefix(value):
    text = str(value or "").strip()
    if not text:
        return "", ""
    normalized = text.replace("＋", "+")
    compact = re.sub(r"[\s\-()（）]", "", normalized)
    if compact.startswith("00"):
        compact = "+" + compact[2:]
    if compact.startswith("+"):
        digits = re.sub(r"\D", "", compact[1:])
        for prefix in sorted(PHONE_PREFIX_VALUES, key=len, reverse=True):
            code = prefix[1:]
            if digits.startswith(code):
                return prefix, digits[len(code) :]
        return "", digits
    digits = re.sub(r"\D", "", compact)
    if digits.startswith("86") and CHINA_MOBILE_RE.match(digits[2:]):
        return "+86", digits[2:]
    if CHINA_MOBILE_RE.match(digits):
        return "+86", digits
    if NANP_WITH_CODE_RE.match(digits):
        return "+1", digits[1:]
    if NANP_LOCAL_RE.match(digits):
        return "+1", digits
    for prefix in sorted(PHONE_PREFIX_VALUES, key=len, reverse=True):
        code = prefix[1:]
        if digits.startswith(code) and len(digits) > len(code) + 5:
            return prefix, digits[len(code) :]
    return "", text


def format_phone_with_prefix(prefix, number):
    prefix = str(prefix or "").strip()
    number = str(number or "").strip()
    if not number:
        return ""
    if not prefix:
        return number
    local = re.sub(r"[\s\-()（）]", "", number)
    if local.startswith(prefix.replace("+", "")):
        local = local[len(prefix.replace("+", "")) :]
    return f"{prefix} {local}" if local else prefix


def normalize_phone_number(value, default_prefix=""):
    text = str(value or "").strip()
    if not text:
        return ""

    parts = [part for part in PHONE_SPLIT_RE.split(text) if part]
    if len(parts) > 1:
        return " / ".join(normalize_phone_number(part, default_prefix=default_prefix) for part in parts)

    normalized = text.replace("＋", "+")
    compact = re.sub(r"[\s\-()（）]", "", normalized)
    if compact.startswith("00"):
        compact = "+" + compact[2:]

    if compact.startswith("+"):
        digits = re.sub(r"\D", "", compact[1:])
        for prefix in sorted(PHONE_PREFIX_VALUES, key=len, reverse=True):
            code = prefix[1:]
            if digits.startswith(code):
                return format_phone_with_prefix(prefix, digits[len(code) :])
        return f"+{digits}" if digits else text

    digits = re.sub(r"\D", "", compact)
    if default_prefix and digits:
        return format_phone_with_prefix(default_prefix, digits)
    if digits.startswith("86") and CHINA_MOBILE_RE.match(digits[2:]):
        return f"+86 {digits[2:]}"
    if CHINA_MOBILE_RE.match(digits):
        return f"+86 {digits}"
    if NANP_WITH_CODE_RE.match(digits):
        return format_phone_with_prefix("+1", digits[1:])
    if NANP_LOCAL_RE.match(digits):
        return format_phone_with_prefix("+1", digits)
    for prefix in sorted(PHONE_PREFIX_VALUES, key=len, reverse=True):
        code = prefix[1:]
        if digits.startswith(code) and len(digits) > len(code) + 5:
            return format_phone_with_prefix(prefix, digits[len(code) :])
    if default_prefix and digits:
        return format_phone_with_prefix(default_prefix, digits)
    return text


def normalize_phone_number_for_region(value, *hints):
    prefix = infer_phone_prefix(*hints)
    return normalize_phone_number(value, default_prefix=prefix)


def clean_wechat_value(value):
    text = str(value or "").strip().strip(" :：,，;；/、")
    text = re.sub(r"^(?:v|vx|wx|wechat|weixin|微信号?|微.?信)\s*[:：=]?\s*", "", text, flags=re.IGNORECASE)
    return text.strip().strip(" :：,，;；/、")


def merge_wechat_values(*values):
    result = []
    seen = set()
    for value in values:
        if value in ("", None):
            continue
        for part in PHONE_SPLIT_RE.split(str(value)):
            item = clean_wechat_value(part)
            if not item:
                continue
            key = item.lower()
            if key not in seen:
                seen.add(key)
                result.append(item)
    return " / ".join(result)


def _is_phone_like_value(value):
    text = str(value or "").strip().replace("＋", "+")
    digits = re.sub(r"\D", "", text)
    if len(digits) < 7:
        return False
    if text.startswith(("+", "00")):
        return True
    if CHINA_MOBILE_RE.match(digits) or (digits.startswith("86") and CHINA_MOBILE_RE.match(digits[2:])):
        return True
    return len(digits) >= 9


def merge_phone_values(*values, hints=()):
    result = []
    seen = set()
    for value in values:
        if value in ("", None):
            continue
        for part in PHONE_SPLIT_RE.split(str(value)):
            if not _is_phone_like_value(part):
                continue
            item = normalize_phone_number_for_region(part, *hints)
            key = re.sub(r"\D", "", item)
            if key and key not in seen:
                seen.add(key)
                result.append(item)
    return " / ".join(result)


def _is_wechat_token(value):
    token = clean_wechat_value(value)
    if not token or "@" in token:
        return False
    if token.lower() in NON_WECHAT_TOKENS:
        return False
    return bool(re.search(r"[A-Za-z]", token))


def _is_probable_wechat_identifier(value):
    token = clean_wechat_value(value)
    if not _is_wechat_token(token):
        return False
    if re.search(r"[\s\u4e00-\u9fff]", token):
        return False
    return bool(re.search(r"[A-Za-z]", token) and re.search(r"\d", token))


def _strip_spans(text, spans):
    if not spans:
        return text
    chunks = []
    cursor = 0
    for start, end in sorted(spans):
        chunks.append(text[cursor:start])
        cursor = end
    chunks.append(text[cursor:])
    return " ".join(part for part in chunks if part).strip()


def _clean_contact_noise_from_name(value):
    text = str(value or "").strip()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s*([,，;；/、|｜]+)\s*", " ", text)
    text = re.sub(r"(?:电话|手机|手机号|客户电话|联系电话|tel|phone|mobile|whatsapp|wa|v|vx|wx|wechat|weixin|微信号?|微.?信)\s*[:：=]?\s*$", "", text, flags=re.IGNORECASE)
    return text.strip().strip(" :：,，;；/、|｜-—_")


def _is_probable_region_suffix(value):
    text = str(value or "").strip()
    if not re.fullmatch(r"[\u4e00-\u9fff]{2,12}", text):
        return False
    if any(word in text for word in NON_REGION_SUFFIX_WORDS):
        return False
    for _prefix, keywords in PHONE_PREFIX_BY_HINT:
        if text in keywords:
            return True
    for _prefix, label in PHONE_PREFIX_CHOICES:
        if text in _phone_prefix_label_keywords(label):
            return True
    return False


def split_region_from_customer_name(value):
    text = str(value or "").strip()
    if not text:
        return "", ""
    for pattern in (REGION_PAREN_SUFFIX_RE, REGION_SEPARATOR_SUFFIX_RE):
        match = pattern.match(text)
        if not match:
            continue
        name = match.group("name").strip().strip(" :：,，;；/、|｜-—_")
        region = match.group("region").strip()
        if name and _is_probable_region_suffix(region):
            return name, region
    return text, ""


def clean_customer_name_contacts(value, *hints):
    text = str(value or "").strip()
    if not text:
        return "", "", ""

    found_phone = []
    found_wechat = []
    contact_removed = False
    remove_spans = []
    for match in WECHAT_MARKER_RE.finditer(text):
        candidate = clean_wechat_value(match.group(1))
        if candidate:
            found_wechat.append(candidate)
            remove_spans.append(match.span())
            contact_removed = True
    working = _strip_spans(text, remove_spans)

    remove_spans = []
    for match in PHONE_MARKER_RE.finditer(working):
        candidate = match.group(1)
        if _is_phone_like_value(candidate):
            found_phone.append(candidate)
            remove_spans.append(match.span())
            contact_removed = True
    working = _strip_spans(working, remove_spans)

    remove_spans = []
    for match in PHONE_IN_TEXT_RE.finditer(working):
        candidate = match.group(0)
        if _is_phone_like_value(candidate):
            found_phone.append(candidate)
            remove_spans.append(match.span())
            contact_removed = True
    working = _strip_spans(working, remove_spans)

    cleaned = _clean_contact_noise_from_name(working) if contact_removed else text.strip()
    if cleaned and _is_probable_wechat_identifier(cleaned):
        found_wechat.append(cleaned)
        cleaned = ""

    return (
        cleaned,
        merge_phone_values(*found_phone, hints=hints),
        merge_wechat_values(*found_wechat),
    )


def split_phone_and_wechat(value, *hints):
    text = str(value or "").strip()
    if not text:
        return "", ""

    found_wechat = []
    remove_spans = []
    for match in WECHAT_MARKER_RE.finditer(text):
        candidate = clean_wechat_value(match.group(1))
        if candidate:
            found_wechat.append(candidate)
            remove_spans.append(match.span())

    working = _strip_spans(text, remove_spans)
    if not found_wechat:
        token_matches = [match for match in WECHAT_TOKEN_RE.finditer(working) if _is_wechat_token(match.group(0))]
        if token_matches:
            found_wechat.extend(match.group(0) for match in token_matches)
            working = _strip_spans(working, [match.span() for match in token_matches])

    if re.search(r"[A-Za-z]", text) and not re.search(r"\d{7,}", text):
        wechat = merge_wechat_values(*(found_wechat or [text]))
        return "", wechat

    phone = normalize_phone_number_for_region(working, *hints)
    if found_wechat and len(re.sub(r"\D", "", phone)) < 6:
        phone = ""
    return phone, merge_wechat_values(*found_wechat)


def merge_region_city(region, city):
    region = str(region or "").strip()
    city = str(city or "").strip()
    if not region:
        return city
    if not city or city == region or city in region:
        return region
    if region in city:
        return city
    return f"{region} {city}".strip()


def display_owner_name(value, resolve_user=False):
    text = str(value or "").strip()
    if not text:
        return ""
    alias = OWNER_CODE_NAME_ALIASES.get(text.upper())
    if alias:
        return OWNER_NAME_ALIASES.get(alias, alias)
    if resolve_user:
        user = User.objects.filter(username__iexact=text, is_active=True).only("username", "first_name", "last_name").first()
        if user:
            user_name = user.get_full_name() or f"{user.last_name}{user.first_name}".strip() or user.username
            return OWNER_NAME_ALIASES.get(user_name, user_name)
    return OWNER_NAME_ALIASES.get(text, text)

def resolve_user_by_display_name(value):
    text = str(value or "").strip()
    if not text:
        return None
    users = User.objects.filter(is_active=True)
    for user in users:
        candidates = {
            user.username,
            user.get_full_name(),
            f"{user.last_name}{user.first_name}".strip(),
            f"{user.first_name}{user.last_name}".strip(),
        }
        if text in {candidate for candidate in candidates if candidate}:
            return user
    return None


def _safe_feishu_username(display_name, feishu_open_id):
    seed = feishu_open_id or display_name or "feishu"
    suffix = hashlib.sha1(str(seed).encode("utf-8")).hexdigest()[:10]
    prefix = re.sub(r"[^A-Za-z0-9_.@+-]+", "_", str(display_name or "")).strip("_").lower()
    if not prefix:
        prefix = "feishu"
    return f"{prefix[:40]}_{suffix}"


def resolve_or_create_user_by_feishu(display_name="", feishu_open_id="", email=""):
    display_name = str(display_name or "").strip()
    feishu_open_id = str(feishu_open_id or "").strip()
    email = str(email or "").strip()
    if display_name in PLACEHOLDER_USER_NAMES and not (feishu_open_id or email):
        return None
    user = None

    if feishu_open_id:
        profile = Profile.objects.select_related("user").filter(feishu_open_id=feishu_open_id).first()
        if profile:
            return profile.user

    if email:
        user = User.objects.filter(email__iexact=email).first()
    if not user and display_name:
        user = resolve_user_by_display_name(display_name)
    if not user and not (display_name or feishu_open_id or email):
        return None

    if not user:
        username = _safe_feishu_username(display_name or email, feishu_open_id or email)
        base_username = username
        index = 2
        while User.objects.filter(username=username).exists():
            username = f"{base_username}_{index}"
            index += 1
        user = User(username=username, email=email, first_name=display_name[:150])
        user.set_unusable_password()
        user.save()

    profile, _ = Profile.objects.get_or_create(user=user)
    update_fields = []
    if feishu_open_id and profile.feishu_open_id != feishu_open_id:
        profile.feishu_open_id = feishu_open_id
        update_fields.append("feishu_open_id")
    if update_fields:
        profile.save(update_fields=update_fields)
    return user


class Profile(models.Model):
    class Role(models.TextChoices):
        ADMIN = "admin", "管理员"
        LEADER = "leader", "领导"
        SALES = "sales", "销售"
        MARKETING = "marketing", "新媒体"
        FINANCE = "finance", "财务"
        TECHNICIAN = "technician", "技术人员"

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="profile", verbose_name="用户")
    role = models.CharField("角色", max_length=20, choices=Role.choices, default=Role.SALES)
    feishu_open_id = models.CharField("飞书用户标识", max_length=128, blank=True)
    active = models.BooleanField("启用", default=True)

    class Meta:
        verbose_name = "员工权限"
        verbose_name_plural = "员工权限"

    def __str__(self):
        return f"{self.user.get_username()} / {self.get_role_display()}"


class Tag(models.Model):
    name = models.CharField("标签名称", max_length=64, unique=True)
    category = models.CharField("分类", max_length=32, blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        ordering = ["category", "name"]
        verbose_name = "标签"
        verbose_name_plural = "标签"

    def __str__(self):
        return self.name


class Customer(models.Model):
    class RecordKind(models.TextChoices):
        LEAD = "lead", "线索"
        CUSTOMER = "customer", "客户"

    class Grade(models.TextChoices):
        INCUBATING = "incubating", "待孵化客户"
        POTENTIAL = "potential", "潜在客户"
        NORMAL = "normal", "一般客户"
        INTENTION = "intention", "意向客户"
        KEY = "key", "重点客户"
        UNCERTAIN = "uncertain", "待定客户"
        INVALID = "invalid", "无效客户"

    class Status(models.TextChoices):
        PRIVATE = "private", "私有客户"
        PUBLIC = "public", "公海客户"
        INVALID = "invalid", "无效客户"
        DEAL = "deal", "成交客户"

    class CustomerLevel(models.TextChoices):
        PENDING = "pending", "待定"
        NORMAL = "normal", "一般客户"
        INTENTION = "intention", "意向客户"
        NO_INTENT = "no_intent", "无意向"
        INVALID = "invalid", "无效"
        DEAL = "deal", "已成交"

    class FollowStatus(models.TextChoices):
        NEW_INQUIRY = "new_inquiry", "新询盘"
        CONTACTED = "contacted", "已联系"
        CONTACT_ADDED = "contact_added", "已加联系方式"
        DEMAND_CONFIRMING = "demand_confirming", "需求确认中"
        NOT_QUOTED = "not_quoted", "未报价"
        QUOTING = "quoting", "报价中"
        QUOTED = "quoted", "已报价"
        SAMPLE_TESTING = "sample_testing", "样罐测试中"
        CONTRACTING = "contracting", "合同中"
        PAYMENT_PENDING = "payment_pending", "待收款"
        DEAL = "deal", "已成交"
        PAUSED = "paused", "暂停跟进"
        INVALID_CLOSED = "invalid_closed", "无效关闭"

    class DealStatus(models.TextChoices):
        OPEN = "open", "未成交"
        WON = "won", "已成交"
        CANCELED = "canceled", "取消"

    class TradeType(models.TextChoices):
        DOMESTIC = "domestic", "内贸"
        FOREIGN = "foreign", "外贸"

    customer_no = models.CharField("客户编号", max_length=32, unique=True, blank=True)
    legacy_customer_no = models.CharField("历史客户编号", max_length=64, blank=True)
    lead_no = models.CharField("线索编号", max_length=64, blank=True)
    source_kind = models.CharField("资料类型", max_length=20, choices=RecordKind.choices, default=RecordKind.CUSTOMER)
    name = models.CharField("客户名称", max_length=160, blank=True)
    original_name = models.CharField("原始客户名称", max_length=200, blank=True)
    nickname = models.CharField("客户昵称/网名", max_length=120, blank=True, db_index=True)
    official_name = models.CharField("客户正式名称", max_length=200, blank=True, db_index=True)
    short_name = models.CharField("客户简称", max_length=120, blank=True)
    company_name = models.CharField("公司名称", max_length=200, blank=True)
    main_contact_name = models.CharField("主联系人姓名", max_length=80, blank=True)
    contact_name = models.CharField("联系人", max_length=80, blank=True)
    contact_position = models.CharField("联系人职位", max_length=80, blank=True)
    phone = models.CharField("客户电话", max_length=80, blank=True)
    wechat = models.CharField("微信", max_length=120, blank=True)
    whatsapp = models.CharField("WhatsApp", max_length=120, blank=True, db_index=True)
    email = models.CharField("邮箱", max_length=254, blank=True)
    instagram = models.CharField("Instagram", max_length=120, blank=True)
    facebook = models.CharField("Facebook", max_length=120, blank=True)
    platform_account = models.CharField("外贸平台账号/阿里账号", max_length=160, blank=True)
    region = models.CharField("地区", max_length=160, blank=True)
    city = models.CharField("城市", max_length=80, blank=True)
    country_region = models.CharField("国家/地区", max_length=160, blank=True, db_index=True)
    province_city = models.CharField("省市地区", max_length=160, blank=True)
    language = models.CharField("沟通语言", max_length=60, blank=True)
    timezone = models.CharField("时区", max_length=60, blank=True)
    industry = models.CharField("行业", max_length=120, blank=True)
    source_channel = models.CharField("线索来源", max_length=80, blank=True, db_index=True)
    account_source = models.CharField("账号来源", max_length=80, blank=True)
    trade_type = models.CharField("内贸/外贸", max_length=20, choices=TradeType.choices, blank=True, db_index=True)
    customer_type = models.CharField("客户类型", max_length=80, blank=True, db_index=True)
    customer_level = models.CharField("当前级别", max_length=20, choices=CustomerLevel.choices, default=CustomerLevel.PENDING, db_index=True)
    follow_status = models.CharField("当前跟进状态", max_length=32, choices=FollowStatus.choices, default=FollowStatus.NEW_INQUIRY, db_index=True)
    deal_status = models.CharField("成交状态", max_length=20, choices=DealStatus.choices, default=DealStatus.OPEN, db_index=True)
    demand = models.TextField("客户需求", blank=True)
    product_interest = models.TextField("关注产品", blank=True)
    demand_summary = models.TextField("客户需求摘要", blank=True)
    equipment_model = models.CharField("设备型号", max_length=120, blank=True)
    capacity_requirement = models.CharField("产能需求", max_length=120, blank=True)
    can_type = models.CharField("罐型", max_length=120, blank=True)
    sample_can_info = models.TextField("样罐信息", blank=True)
    is_carbonated = models.BooleanField("是否含气", default=False)
    need_sample_test = models.BooleanField("是否需要样罐测试", default=False)
    lead_status = models.CharField("线索状态", max_length=40, blank=True)
    related_lead = models.CharField("关联线索", max_length=120, blank=True)
    customer_status_text = models.TextField("客户状态", blank=True)
    grade = models.CharField("客户级别", max_length=20, choices=Grade.choices, default=Grade.POTENTIAL)
    status = models.CharField("客户归属", max_length=20, choices=Status.choices, default=Status.PRIVATE)
    is_deal = models.BooleanField("成交状态", default=False)
    owner = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="customers", verbose_name="客户经理")
    co_owners = models.ManyToManyField(User, blank=True, related_name="collaborating_customers", verbose_name="协作业务员")
    owner_name = models.CharField("客户经理名称", max_length=120, blank=True)
    created_by_name = models.CharField("创建人名称", max_length=120, blank=True)
    tags = models.ManyToManyField(Tag, blank=True, related_name="customers", verbose_name="标签")
    notes = models.TextField("沟通记录/备注", blank=True)
    duplicate_checked = models.BooleanField("已检查重复", default=False)
    duplicate_customer_no = models.CharField("重复客户编号", max_length=64, blank=True)
    attachment_note = models.TextField("附件说明", blank=True)
    attachment_file = models.FileField("客户附件", upload_to="contract_attachments/%Y/%m", blank=True, max_length=300)
    image = models.FileField("客户图片", upload_to="customer_images/%Y/%m/", blank=True)
    last_contact_at = models.DateTimeField("最后联系时间", null=True, blank=True)
    next_contact_at = models.DateTimeField("下次联系时间", null=True, blank=True)
    next_follow_at = models.DateTimeField("下次跟进时间", null=True, blank=True, db_index=True)
    next_action = models.CharField("下一步动作", max_length=200, blank=True)
    expected_close_month = models.CharField("预计成交月份", max_length=20, blank=True, db_index=True)
    is_fast_deal = models.BooleanField("是否快成交", default=False, db_index=True)
    release_warned_at = models.DateTimeField("公海提醒时间", null=True, blank=True)
    is_public = models.BooleanField("是否公海客户", default=False, db_index=True)
    public_at = models.DateTimeField("进入公海时间", null=True, blank=True)
    is_recycled = models.BooleanField("是否进入回收站", default=False, db_index=True)
    recycled_at = models.DateTimeField("进入回收站时间", null=True, blank=True)
    recycle_reason = models.CharField("回收原因", max_length=240, blank=True)
    historical_created_at = models.DateTimeField("历史创建时间", null=True, blank=True)
    original_assigned_at = models.DateTimeField("原始分配时间", null=True, blank=True)
    feishu_source_name = models.CharField("飞书来源名称", max_length=120, blank=True)
    feishu_app_token = models.CharField("飞书应用令牌", max_length=128, blank=True)
    feishu_table_id = models.CharField("飞书表格标识", max_length=64, blank=True)
    feishu_record_id = models.CharField("飞书记录标识", max_length=64, blank=True)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="created_customers", verbose_name="系统创建人")
    updated_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="updated_customers", verbose_name="最后更新人")
    created_at = models.DateTimeField("系统录入时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)
    is_active = models.BooleanField("有效", default=True)

    class Meta:
        ordering = ["-updated_at"]
        verbose_name = "客户"
        verbose_name_plural = "客户"
        permissions = [
            ("view_all_customers", "可以查看全部客户"),
            ("assign_customer", "可以分配客户"),
        ]

    @classmethod
    def next_customer_no(cls):
        max_number = 0
        for value in cls.objects.filter(customer_no__startswith=CUSTOMER_NO_PREFIX).values_list("customer_no", flat=True):
            text = str(value or "").strip()
            if is_system_customer_no(text):
                max_number = max(max_number, int(text[len(CUSTOMER_NO_PREFIX):]))
        return f"{CUSTOMER_NO_PREFIX}{max_number + 1:0{CUSTOMER_NO_WIDTH}d}"

    def save(self, *args, **kwargs):
        creating = not self.pk
        update_fields = set(kwargs["update_fields"]) if kwargs.get("update_fields") is not None else None

        def mark(field_name):
            if update_fields is not None:
                update_fields.add(field_name)
        if not creating:
            original = type(self).objects.filter(pk=self.pk).only("created_at", "historical_created_at").first()
            if original:
                self.created_at = original.created_at
                if original.historical_created_at:
                    self.historical_created_at = original.historical_created_at
                if update_fields is not None:
                    update_fields.discard("created_at")
                    if original.historical_created_at:
                        update_fields.discard("historical_created_at")
        if creating and not self.customer_no:
            self.customer_no = self.next_customer_no()
        if self.official_name and not self.name:
            self.name = self.official_name
            mark("name")
        elif self.name and not self.official_name:
            self.official_name = self.name
            mark("official_name")
        if self.company_name and not self.official_name:
            self.official_name = self.company_name
            mark("official_name")
        if self.main_contact_name and not self.contact_name:
            self.contact_name = self.main_contact_name
            mark("contact_name")
        elif self.contact_name and not self.main_contact_name:
            self.main_contact_name = self.contact_name
            mark("main_contact_name")
        if self.product_interest and not self.demand:
            self.demand = self.product_interest
            mark("demand")
        elif self.demand and not self.product_interest:
            self.product_interest = self.demand
            mark("product_interest")
        if self.demand_summary and not self.notes:
            self.notes = self.demand_summary
            mark("notes")
        if self.next_follow_at and not self.next_contact_at:
            self.next_contact_at = self.next_follow_at
            mark("next_contact_at")
        elif self.next_contact_at and not self.next_follow_at:
            self.next_follow_at = self.next_contact_at
            mark("next_follow_at")
        if self.deal_status == self.DealStatus.WON or self.is_deal or self.status == self.Status.DEAL:
            self.is_deal = True
            self.status = self.Status.DEAL
            self.deal_status = self.DealStatus.WON
            self.customer_level = self.CustomerLevel.DEAL
            self.follow_status = self.FollowStatus.DEAL
            for field_name in ("is_deal", "status", "deal_status", "customer_level", "follow_status"):
                mark(field_name)
        if self.customer_level == self.CustomerLevel.INVALID or self.grade == self.Grade.INVALID or self.status == self.Status.INVALID:
            self.is_recycled = True
            self.status = self.Status.INVALID
            self.follow_status = self.FollowStatus.INVALID_CLOSED
            if not self.recycled_at:
                self.recycled_at = timezone.now()
            for field_name in ("is_recycled", "status", "follow_status", "recycled_at"):
                mark(field_name)
        self.region = merge_region_city(self.region, self.city)
        self.city = ""
        if self.region and not self.country_region:
            self.country_region = self.region
            mark("country_region")
        if self.country_region and not self.region:
            self.region = self.country_region
            mark("region")
        cleaned_name, name_phone, name_wechat = clean_customer_name_contacts(self.name, self.region)
        cleaned_name, name_region = split_region_from_customer_name(cleaned_name)
        if name_region:
            self.region = merge_region_city(self.region, name_region)
        if cleaned_name != self.name:
            self.name = cleaned_name
            if update_fields is not None:
                update_fields.add("name")
        if name_phone:
            self.phone = merge_phone_values(self.phone, name_phone, hints=(self.region, self.name))
        self.wechat = merge_wechat_values(self.wechat, name_wechat)
        self.phone, phone_wechat = split_phone_and_wechat(self.phone, self.region, self.name)
        self.wechat = merge_wechat_values(self.wechat, phone_wechat)
        if update_fields is not None:
            update_fields.update({"phone", "wechat", "region", "city"})
        if self.status == self.Status.PUBLIC:
            if self.owner_id and not self.owner_name:
                self.owner_name = self.owner.get_full_name() or self.owner.username
                if update_fields is not None:
                    update_fields.add("owner_name")
            self.owner = None
            self.is_public = True
            if not self.public_at:
                self.public_at = timezone.now()
            if update_fields is not None:
                update_fields.update({"owner", "is_public", "public_at"})
        elif not self.owner_id and self.owner_name:
            self.owner = resolve_user_by_display_name(self.owner_name)
            if self.owner_id and update_fields is not None:
                update_fields.add("owner")
        for field in self._meta.fields:
            max_length = getattr(field, "max_length", None)
            value = getattr(self, field.name, None)
            if max_length and isinstance(value, str) and len(value) > max_length:
                setattr(self, field.name, value[:max_length])
                if update_fields is not None:
                    update_fields.add(field.name)
        if update_fields is not None:
            kwargs["update_fields"] = sorted(update_fields)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name or self.contact_name or self.customer_no

    @property
    def owner_display(self):
        if self.owner_id:
            return display_owner_name(self.owner.get_full_name() or self.owner.username)
        return display_owner_name(self.owner_name)

    @property
    def location_display(self):
        return merge_region_city(self.region, self.city)

    @property
    def created_time_display(self):
        return self.historical_created_at or self.created_at

    @property
    def uncontacted_days(self):
        if not self.last_contact_at:
            return None
        return (timezone.localdate() - timezone.localtime(self.last_contact_at).date()).days

    @property
    def next_follow_time(self):
        return self.next_follow_at or self.next_contact_at

    @property
    def display_name(self):
        return self.nickname or self.official_name or self.name or self.company_name or self.customer_no

    @property
    def latest_quote_amount(self):
        try:
            quote = self.quotes.order_by("-quote_date", "-created_at").first()
        except Exception:
            return Decimal("0")
        return quote.total_amount if quote else Decimal("0")

    @property
    def total_contract_amount(self):
        try:
            total = self.contracts.filter(is_active=True).aggregate(total=models.Sum("contract_amount"))["total"]
            if total in (None, Decimal("0")):
                total = self.contracts.filter(is_active=True).aggregate(total=models.Sum("amount"))["total"]
        except Exception:
            return Decimal("0")
        return total or Decimal("0")

    @property
    def paid_amount(self):
        try:
            return self.payments.aggregate(total=models.Sum("actual_received_amount"))["total"] or Decimal("0")
        except Exception:
            return Decimal("0")

    @property
    def unpaid_amount(self):
        amount = self.total_contract_amount - self.paid_amount
        return amount if amount > 0 else Decimal("0")


class ArchivedCustomerSnapshot(models.Model):
    batch_id = models.CharField("批次标识", max_length=64, db_index=True)
    original_customer_id = models.BigIntegerField("原客户记录标识")
    customer_no = models.CharField("客户编号", max_length=64, blank=True, db_index=True)
    name = models.CharField("客户名称", max_length=160, blank=True)
    source_kind = models.CharField("资料类型", max_length=20, blank=True)
    reason = models.CharField("归档原因", max_length=160, blank=True)
    payload = models.JSONField("客户快照", default=dict)
    related_payload = models.JSONField("关联数据快照", default=dict, blank=True)
    archived_at = models.DateTimeField("归档时间", auto_now_add=True)

    class Meta:
        ordering = ["-archived_at", "customer_no"]
        verbose_name = "客户归档快照"
        verbose_name_plural = "客户归档快照"

    def __str__(self):
        return f"{self.batch_id} / {self.customer_no or self.original_customer_id}"


class Contact(models.Model):
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="contacts", verbose_name="客户")
    name = models.CharField("联系人姓名", max_length=80)
    position = models.CharField("职位", max_length=80, blank=True)
    phone = models.CharField("电话", max_length=80, blank=True)
    wechat = models.CharField("微信", max_length=120, blank=True)
    whatsapp = models.CharField("WhatsApp", max_length=120, blank=True)
    email = models.CharField("邮箱", max_length=254, blank=True)
    language = models.CharField("沟通语言", max_length=60, blank=True)
    is_primary = models.BooleanField("主联系人", default=False, db_index=True)
    remark = models.TextField("备注", blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        ordering = ["-is_primary", "name"]
        verbose_name = "联系人"
        verbose_name_plural = "联系人"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.is_primary:
            Contact.objects.filter(customer=self.customer).exclude(pk=self.pk).update(is_primary=False)
            updates = {}
            if self.customer.main_contact_name != self.name:
                updates["main_contact_name"] = self.name
                updates["contact_name"] = self.name
            for field in ("phone", "wechat", "whatsapp", "email", "language"):
                value = getattr(self, field)
                if value and not getattr(self.customer, field, ""):
                    updates[field] = value
            if updates:
                Customer.objects.filter(pk=self.customer_id).update(**updates, updated_at=timezone.now())

    def __str__(self):
        return f"{self.customer} / {self.name}"


class Lead(models.Model):
    class Status(models.TextChoices):
        NEW = "new", "新线索"
        PENDING_ASSIGN = "pending_assign", "待分配"
        ASSIGNED = "assigned", "已分配"
        CONTACTED = "contacted", "已联系"
        CONVERTED = "converted", "已转客户"
        DUPLICATE = "duplicate", "重复"
        INVALID = "invalid", "无效"

    lead_no = models.CharField("线索编号", max_length=32, unique=True, blank=True)
    raw_nickname = models.CharField("原始昵称/网名", max_length=160, blank=True)
    customer_name = models.CharField("客户名称", max_length=160, blank=True)
    name = models.CharField("客户名称", max_length=160, blank=True)
    contact_name = models.CharField("联系人", max_length=80, blank=True)
    phone = models.CharField("客户电话", max_length=80, blank=True)
    wechat = models.CharField("微信", max_length=120, blank=True)
    whatsapp = models.CharField("WhatsApp", max_length=120, blank=True)
    email = models.CharField("邮箱", max_length=254, blank=True)
    instagram = models.CharField("Instagram", max_length=120, blank=True)
    facebook = models.CharField("Facebook", max_length=120, blank=True)
    region = models.CharField("地区", max_length=160, blank=True)
    country_region = models.CharField("国家/地区", max_length=160, blank=True)
    language = models.CharField("沟通语言", max_length=60, blank=True)
    trade_type = models.CharField("内贸/外贸", max_length=20, choices=Customer.TradeType.choices, blank=True, db_index=True)
    source_channel = models.CharField("线索来源", max_length=80, blank=True, db_index=True)
    customer_type = models.CharField("客户类型", max_length=80, blank=True)
    demand = models.CharField("客户需求", max_length=160, blank=True)
    product_demand = models.TextField("产品需求", blank=True)
    equipment_model = models.CharField("设备型号", max_length=120, blank=True)
    capacity_requirement = models.CharField("产能", max_length=120, blank=True)
    can_type = models.CharField("罐型", max_length=120, blank=True)
    sample_can_info = models.TextField("样罐", blank=True)
    is_carbonated = models.BooleanField("是否含气", default=False)
    status = models.CharField("线索状态", max_length=20, choices=Status.choices, default=Status.NEW)
    owner = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="leads", verbose_name="负责人")
    co_owners = models.ManyToManyField(User, blank=True, related_name="collaborating_leads", verbose_name="协作负责人")
    related_customer = models.ForeignKey(Customer, null=True, blank=True, on_delete=models.SET_NULL, related_name="leads", verbose_name="关联客户")
    converted_customer = models.ForeignKey(Customer, null=True, blank=True, on_delete=models.SET_NULL, related_name="converted_leads", verbose_name="转化客户")
    tags = models.ManyToManyField(Tag, blank=True, related_name="leads", verbose_name="标签")
    notes = models.TextField("备注", blank=True)
    next_contact_at = models.DateTimeField("下次联系时间", null=True, blank=True)
    assigned_at = models.DateTimeField("分配时间", null=True, blank=True)
    first_contact_at = models.DateTimeField("首次联系时间", null=True, blank=True)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="created_leads", verbose_name="系统创建人")
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)
    is_active = models.BooleanField("有效", default=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "线索"
        verbose_name_plural = "线索"
        permissions = [
            ("view_all_leads", "可以查看全部线索"),
            ("assign_lead", "可以分配线索"),
        ]

    @classmethod
    def next_customer_no(cls):
        max_number = 0
        for value in cls.objects.filter(customer_no__startswith=CUSTOMER_NO_PREFIX).values_list("customer_no", flat=True):
            text = str(value or "").strip()
            if is_system_customer_no(text):
                max_number = max(max_number, int(text[len(CUSTOMER_NO_PREFIX):]))
        return f"{CUSTOMER_NO_PREFIX}{max_number + 1:0{CUSTOMER_NO_WIDTH}d}"

    def save(self, *args, **kwargs):
        creating = not self.pk
        if creating and not self.lead_no:
            self.lead_no = next_monthly_no(type(self), "lead_no", "XS")
        if self.customer_name and not self.name:
            self.name = self.customer_name
        elif self.name and not self.customer_name:
            self.customer_name = self.name
        if self.product_demand and not self.demand:
            self.demand = self.product_demand[:160]
        elif self.demand and not self.product_demand:
            self.product_demand = self.demand
        if self.region and not self.country_region:
            self.country_region = self.region
        cleaned_name, name_phone, name_wechat = clean_customer_name_contacts(self.name, self.region)
        cleaned_name, name_region = split_region_from_customer_name(cleaned_name)
        if name_region:
            self.region = merge_region_city(self.region, name_region)
        if cleaned_name != self.name:
            self.name = cleaned_name
        if name_phone:
            self.phone = merge_phone_values(self.phone, name_phone, hints=(self.region, self.name))
        self.wechat = merge_wechat_values(self.wechat, name_wechat)
        self.phone, phone_wechat = split_phone_and_wechat(self.phone, self.region, self.name)
        self.wechat = merge_wechat_values(self.wechat, phone_wechat)
        update_fields = set(kwargs["update_fields"]) if kwargs.get("update_fields") is not None else None

        def mark(field_name):
            if update_fields is not None:
                update_fields.add(field_name)
        if update_fields is not None:
            update_fields.update({"name", "phone", "wechat", "region"})
            kwargs["update_fields"] = sorted(update_fields)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name or self.contact_name or self.lead_no


class ContactLog(models.Model):
    class Method(models.TextChoices):
        PHONE = "phone", "电话"
        WECHAT = "wechat", "微信"
        WHATSAPP = "whatsapp", "WhatsApp"
        EMAIL = "email", "邮箱"
        PLATFORM = "platform", "平台消息"
        VIDEO = "video", "视频会议"
        VISIT = "visit", "到访"
        OTHER = "other", "其他"

    class Source(models.TextChoices):
        MANUAL = "manual", "人工录入"
        XIAOQUAN = "xiaoquan", "CRM助手整理"
        FEISHU = "feishu", "飞书同步"
        IMPORT = "import", "批量导入"

    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="contact_logs", verbose_name="客户")
    lead = models.ForeignKey(Lead, null=True, blank=True, on_delete=models.SET_NULL, related_name="contact_logs", verbose_name="线索")
    contact_person = models.CharField("沟通联系人", max_length=120, blank=True)
    followed_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="follow_up_logs", verbose_name="跟进人账号")
    contact_at = models.DateTimeField("跟进时间", default=timezone.now)
    method = models.CharField("跟进方式", max_length=20, choices=Method.choices, default=Method.WECHAT)
    channel = models.CharField("沟通渠道", max_length=20, choices=Method.choices, blank=True)
    source = models.CharField("记录来源", max_length=20, choices=Source.choices, default=Source.MANUAL)
    summary = models.TextField("跟进内容", blank=True)
    content = models.TextField("沟通内容", blank=True)
    demand_update = models.TextField("客户需求更新", blank=True)
    quote_update = models.TextField("报价情况", blank=True)
    sample_update = models.TextField("样罐情况", blank=True)
    customer_feedback = models.TextField("客户反馈", blank=True)
    next_action = models.CharField("下一步动作", max_length=200, blank=True)
    level_after = models.CharField("本次沟通后客户级别", max_length=20, choices=Customer.CustomerLevel.choices, blank=True)
    status_after = models.CharField("本次沟通后跟进状态", max_length=32, choices=Customer.FollowStatus.choices, blank=True)
    attachments = models.TextField("附件说明", blank=True)
    follower_name = models.CharField("跟进人", max_length=120, blank=True)
    result = models.CharField("跟进结果", max_length=160, blank=True)
    photo_note = models.TextField("照片说明", blank=True)
    photo_file = models.FileField("跟进照片", upload_to="contact_photos/%Y/%m", blank=True, max_length=300)
    minutes_link = models.CharField("音频文件", max_length=300, blank=True)
    next_contact_at = models.DateTimeField("下次联系时间", null=True, blank=True)
    feishu_source_name = models.CharField("飞书来源名称", max_length=120, blank=True)
    feishu_record_id = models.CharField("飞书记录标识", max_length=64, blank=True)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="contact_logs", verbose_name="系统创建人")
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        ordering = ["-contact_at"]
        verbose_name = "跟进日志"
        verbose_name_plural = "跟进日志"

    def save(self, *args, **kwargs):
        if self.content and not self.summary:
            self.summary = self.content
        elif self.summary and not self.content:
            self.content = self.summary
        if self.channel and not self.method:
            self.method = self.channel
        elif self.method and not self.channel:
            self.channel = self.method
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.customer} / {self.contact_at:%Y-%m-%d}"

    @property
    def photo_file_url(self):
        if not self.photo_file:
            return ""
        return self.photo_file.url

    @property
    def photo_file_name(self):
        if not self.photo_file:
            return ""
        return str(self.photo_file.name or "").rstrip("/").rsplit("/", 1)[-1]
    @property
    def audio_file_url(self):
        value = str(self.minutes_link or "").strip()
        if not value:
            return ""
        if re.match(r"^https?://", value, re.I):
            return value
        return default_storage.url(value)

    @property
    def audio_file_name(self):
        value = str(self.minutes_link or "").strip().rstrip("/")
        if not value:
            return ""
        return value.rsplit("/", 1)[-1]


class Contract(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        SIGNED = "signed", "已签"
        EXECUTING = "executing", "执行中"
        COMPLETED = "completed", "已完成"
        CANCELED = "canceled", "取消"

    class Currency(models.TextChoices):
        RMB = "RMB", "RMB"
        USD = "USD", "USD"
        EUR = "EUR", "EUR"

    contract_no = models.CharField("合同编号", max_length=64, unique=True, blank=True)
    customer = models.ForeignKey(Customer, null=True, blank=True, on_delete=models.SET_NULL, related_name="contracts", verbose_name="客户")
    quote = models.ForeignKey("Quote", null=True, blank=True, on_delete=models.SET_NULL, related_name="contracts", verbose_name="报价")
    opportunity = models.ForeignKey("Opportunity", null=True, blank=True, on_delete=models.SET_NULL, related_name="contracts", verbose_name="商机")
    customer_name = models.CharField("客户名称", max_length=160, blank=True)
    signed_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="signed_contracts", verbose_name="销售人员")
    sales_user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="sales_contracts", verbose_name="销售负责人")
    signed_by_name = models.CharField("销售人员名称", max_length=120, blank=True)
    signed_date = models.DateField("签约日期", null=True, blank=True)
    signed_at = models.DateField("签订日期", null=True, blank=True)
    amount = models.DecimalField("合同金额", max_digits=12, decimal_places=2, default=0)
    contract_amount = models.DecimalField("合同金额", max_digits=14, decimal_places=2, default=0)
    currency = models.CharField("币种", max_length=10, choices=Currency.choices, default=Currency.RMB)
    payment_terms = models.TextField("付款方式", blank=True)
    advance_payment_ratio = models.DecimalField("预付款比例", max_digits=5, decimal_places=2, null=True, blank=True)
    advance_payment_amount = models.DecimalField("预付款金额", max_digits=14, decimal_places=2, null=True, blank=True)
    contract_file = models.FileField("合同文件", upload_to="contract_files/%Y/%m", blank=True, max_length=300)
    status = models.CharField("合同状态", max_length=20, choices=Status.choices, default=Status.SIGNED, db_index=True)
    has_work_order = models.BooleanField("是否已生成小工单", default=False)
    work_order_no = models.CharField("小工单编号", max_length=80, blank=True)
    remark = models.TextField("备注", blank=True)
    attachment_note = models.TextField("附件说明", blank=True)
    attachment_file = models.FileField("合同附件", upload_to="contract_attachments/%Y/%m", blank=True, max_length=300)
    feishu_source_name = models.CharField("飞书来源名称", max_length=120, blank=True)
    feishu_app_token = models.CharField("飞书应用令牌", max_length=128, blank=True)
    feishu_table_id = models.CharField("飞书表格标识", max_length=64, blank=True)
    feishu_record_id = models.CharField("飞书记录标识", max_length=64, blank=True)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="created_contracts", verbose_name="系统创建人")
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)
    is_active = models.BooleanField("有效", default=True)

    class Meta:
        ordering = ["-signed_date", "-updated_at"]
        verbose_name = "合同"
        verbose_name_plural = "合同"
        permissions = [
            ("view_all_contracts", "可以查看全部合同"),
        ]

    @classmethod
    def next_customer_no(cls):
        max_number = 0
        for value in cls.objects.filter(customer_no__startswith=CUSTOMER_NO_PREFIX).values_list("customer_no", flat=True):
            text = str(value or "").strip()
            if is_system_customer_no(text):
                max_number = max(max_number, int(text[len(CUSTOMER_NO_PREFIX):]))
        return f"{CUSTOMER_NO_PREFIX}{max_number + 1:0{CUSTOMER_NO_WIDTH}d}"

    def save(self, *args, **kwargs):
        creating = not self.pk
        if creating and not self.contract_no:
            self.contract_no = next_monthly_no(type(self), "contract_no", "HT")
        if self.amount and not self.contract_amount:
            self.contract_amount = self.amount
        elif self.contract_amount and not self.amount:
            self.amount = self.contract_amount
        if self.signed_date and not self.signed_at:
            self.signed_at = self.signed_date
        elif self.signed_at and not self.signed_date:
            self.signed_date = self.signed_at
        if self.signed_by_id and not self.sales_user_id:
            self.sales_user = self.signed_by
        super().save(*args, **kwargs)
        if self.customer_id and self.status != self.Status.CANCELED:
            Customer.objects.filter(pk=self.customer_id).update(
                is_deal=True,
                status=Customer.Status.DEAL,
                deal_status=Customer.DealStatus.WON,
                customer_level=Customer.CustomerLevel.DEAL,
                follow_status=Customer.FollowStatus.DEAL,
            )

    @property
    def signed_by_display(self):
        if self.signed_by_name:
            return self.signed_by_name
        if self.signed_by_id:
            return self.signed_by.get_full_name() or self.signed_by.get_username()
        return ""

    @property
    def attachment_file_url(self):
        if not self.attachment_file:
            return ""
        return self.attachment_file.url

    @property
    def attachment_file_name(self):
        if not self.attachment_file:
            return ""
        return str(self.attachment_file.name or "").rstrip("/").rsplit("/", 1)[-1]
    @property
    def total_paid_amount(self):
        return self.payments.aggregate(total=models.Sum("actual_received_amount"))["total"] or Decimal("0")

    @property
    def unpaid_amount(self):
        amount = (self.contract_amount or self.amount or Decimal("0")) - self.total_paid_amount
        return amount if amount > 0 else Decimal("0")

    @property
    def payment_status(self):
        if self.unpaid_amount <= 0 and (self.contract_amount or self.amount):
            return "已收齐"
        if self.total_paid_amount > 0:
            return "部分收款"
        return "未收款"
    def __str__(self):
        return self.contract_no or self.customer_name or "合同"


class Opportunity(models.Model):
    class Stage(models.TextChoices):
        NEW_INQUIRY = "new_inquiry", "新询盘"
        CONTACTED = "contacted", "已联系"
        DEMAND = "demand", "需求确认"
        QUOTE = "quote", "方案报价"
        SAMPLE = "sample", "样罐测试"
        CONTRACT = "contract", "合同中"
        WON = "won", "已成交"
        LOST = "lost", "失败"

    class Status(models.TextChoices):
        OPEN = "open", "进行中"
        WON = "won", "赢单"
        LOST = "lost", "输单"
        PAUSED = "paused", "暂停"

    opportunity_no = models.CharField("商机编号", max_length=32, unique=True, blank=True)
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="opportunities", verbose_name="客户")
    owner = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="opportunities", verbose_name="负责人")
    stage = models.CharField("阶段", max_length=24, choices=Stage.choices, default=Stage.NEW_INQUIRY, db_index=True)
    expected_amount = models.DecimalField("预计金额", max_digits=14, decimal_places=2, default=0)
    currency = models.CharField("币种", max_length=10, choices=Contract.Currency.choices, default=Contract.Currency.RMB)
    expected_close_month = models.CharField("预计成交月份", max_length=20, blank=True, db_index=True)
    probability = models.PositiveSmallIntegerField("成交概率", default=0)
    is_fast_deal = models.BooleanField("快成交", default=False, db_index=True)
    source_channel = models.CharField("来源渠道", max_length=80, blank=True)
    product_interest = models.TextField("关注产品", blank=True)
    latest_progress = models.TextField("最新进展", blank=True)
    next_action = models.CharField("下一步动作", max_length=200, blank=True)
    next_follow_at = models.DateTimeField("下次跟进时间", null=True, blank=True)
    status = models.CharField("状态", max_length=20, choices=Status.choices, default=Status.OPEN, db_index=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        verbose_name = "商机"
        verbose_name_plural = "商机"

    def save(self, *args, **kwargs):
        if not self.opportunity_no:
            self.opportunity_no = next_monthly_no(type(self), "opportunity_no", "SJ")
        super().save(*args, **kwargs)

    def __str__(self):
        return self.opportunity_no or str(self.customer)


class Quote(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        SENT = "sent", "已发送"
        VIEWED = "viewed", "客户已看"
        EXPIRED = "expired", "已失效"
        DEAL = "deal", "已成交"

    quote_no = models.CharField("报价编号", max_length=32, unique=True, blank=True)
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="quotes", verbose_name="客户")
    lead = models.ForeignKey(Lead, null=True, blank=True, on_delete=models.SET_NULL, related_name="quotes", verbose_name="线索")
    opportunity = models.ForeignKey(Opportunity, null=True, blank=True, on_delete=models.SET_NULL, related_name="quotes", verbose_name="商机")
    quote_date = models.DateField("报价日期", default=timezone.localdate, db_index=True)
    quoted_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="quotes", verbose_name="报价人")
    status = models.CharField("报价状态", max_length=20, choices=Status.choices, default=Status.DRAFT, db_index=True)
    currency = models.CharField("币种", max_length=10, choices=Contract.Currency.choices, default=Contract.Currency.RMB)
    total_amount = models.DecimalField("报价总额", max_digits=14, decimal_places=2, default=0)
    valid_until = models.DateField("有效期至", null=True, blank=True)
    attachment = models.FileField("报价单附件", upload_to="quote_attachments/%Y/%m", blank=True, max_length=300)
    remark = models.TextField("备注", blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        ordering = ["-quote_date", "-created_at"]
        verbose_name = "报价"
        verbose_name_plural = "报价"

    def save(self, *args, **kwargs):
        if not self.quote_no:
            self.quote_no = next_monthly_no(type(self), "quote_no", "BJ")
        super().save(*args, **kwargs)
        if self.customer_id and self.status in {self.Status.SENT, self.Status.VIEWED, self.Status.DEAL}:
            follow_status = Customer.FollowStatus.DEAL if self.status == self.Status.DEAL else Customer.FollowStatus.QUOTED
            Customer.objects.filter(pk=self.customer_id).update(follow_status=follow_status, updated_at=timezone.now())

    @property
    def attachment_url(self):
        return self.attachment.url if self.attachment else ""

    def __str__(self):
        return self.quote_no or str(self.customer)

class QuotePlan(models.Model):
    quote = models.ForeignKey(Quote, on_delete=models.CASCADE, related_name="plans", verbose_name="报价")
    plan_name = models.CharField("方案名称", max_length=120)
    equipment_model = models.CharField("设备型号", max_length=120, blank=True)
    capacity = models.CharField("产能", max_length=120, blank=True)
    main_machine_config = models.TextField("主机配置", blank=True)
    can_type = models.CharField("罐型", max_length=120, blank=True)
    is_carbonated = models.BooleanField("是否含气", default=False)
    price = models.DecimalField("单价", max_digits=14, decimal_places=2, default=0)
    quantity = models.PositiveIntegerField("数量", default=1)
    subtotal = models.DecimalField("小计", max_digits=14, decimal_places=2, default=0)
    remark = models.TextField("备注", blank=True)

    class Meta:
        ordering = ["id"]
        verbose_name = "报价方案"
        verbose_name_plural = "报价方案"

    def save(self, *args, **kwargs):
        self.subtotal = (self.price or Decimal("0")) * Decimal(self.quantity or 0)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.plan_name


class QuoteItem(models.Model):
    class ItemType(models.TextChoices):
        MAIN = "main", "主机"
        CHANGEOVER = "changeover", "换型件"
        PRESS_HEAD = "press_head", "压头"
        ROLLER = "roller", "滚轮"
        CAN_TABLE = "can_table", "托罐台"
        OTHER = "other", "其他"

    quote_plan = models.ForeignKey(QuotePlan, on_delete=models.CASCADE, related_name="items", verbose_name="报价方案")
    item_type = models.CharField("明细类型", max_length=24, choices=ItemType.choices, default=ItemType.MAIN)
    item_name = models.CharField("名称", max_length=160)
    specification = models.CharField("规格", max_length=200, blank=True)
    quantity = models.PositiveIntegerField("数量", default=1)
    unit_price = models.DecimalField("单价", max_digits=14, decimal_places=2, default=0)
    subtotal = models.DecimalField("小计", max_digits=14, decimal_places=2, default=0)
    remark = models.TextField("备注", blank=True)

    class Meta:
        ordering = ["id"]
        verbose_name = "报价明细"
        verbose_name_plural = "报价明细"

    def save(self, *args, **kwargs):
        self.subtotal = (self.unit_price or Decimal("0")) * Decimal(self.quantity or 0)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.item_name

class SampleTest(models.Model):
    class ReceiveStatus(models.TextChoices):
        PENDING_SEND = "pending_send", "待寄"
        SENT = "sent", "已寄"
        RECEIVED = "received", "已收到"

    class TechnicalJudgement(models.TextChoices):
        CAN_DO = "can_do", "可做"
        CANNOT_DO = "cannot_do", "不可做"
        NEED_PARTS = "need_parts", "需换型件"
        NEED_SITE = "need_site", "需现场看设备界面"

    class TestResult(models.TextChoices):
        SUCCESS = "success", "成功"
        FAILED = "failed", "失败"
        PENDING = "pending", "待确认"

    sample_no = models.CharField("样罐编号", max_length=32, unique=True, blank=True)
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="sample_tests", verbose_name="客户")
    quote = models.ForeignKey(Quote, null=True, blank=True, on_delete=models.SET_NULL, related_name="sample_tests", verbose_name="报价")
    can_volume = models.CharField("罐容量", max_length=80, blank=True)
    can_type = models.CharField("罐型", max_length=120, blank=True)
    is_carbonated = models.BooleanField("是否含气", default=False)
    receive_status = models.CharField("收样状态", max_length=20, choices=ReceiveStatus.choices, default=ReceiveStatus.PENDING_SEND)
    received_at = models.DateTimeField("收到时间", null=True, blank=True)
    need_sealing_test = models.BooleanField("是否试封", default=False)
    need_filling_test = models.BooleanField("是否试灌", default=False)
    need_video = models.BooleanField("是否拍视频给客户", default=False)
    technical_judgement = models.CharField("技术判断", max_length=24, choices=TechnicalJudgement.choices, blank=True)
    required_changeover_parts = models.TextField("需要换型件", blank=True)
    test_result = models.CharField("测试结果", max_length=20, choices=TestResult.choices, default=TestResult.PENDING)
    test_video = models.FileField("测试视频", upload_to="sample_videos/%Y/%m", blank=True, max_length=300)
    test_images = models.FileField("测试图片", upload_to="sample_images/%Y/%m", blank=True, max_length=300)
    technician = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="sample_tests", verbose_name="技术人员")
    next_action = models.CharField("下一步动作", max_length=200, blank=True)
    remark = models.TextField("备注", blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        verbose_name = "样罐/样品测试"
        verbose_name_plural = "样罐/样品测试"

    def save(self, *args, **kwargs):
        if not self.sample_no:
            self.sample_no = next_monthly_no(type(self), "sample_no", "YG")
        super().save(*args, **kwargs)

    def __str__(self):
        return self.sample_no or str(self.customer)


class Payment(models.Model):
    class PaymentStage(models.TextChoices):
        ADVANCE = "advance", "预付款"
        BEFORE_SHIPMENT = "before_shipment", "发货前尾款"
        ACCEPTANCE = "acceptance", "安装验收款"
        OTHER = "other", "其他"

    payment_no = models.CharField("收款编号", max_length=32, unique=True, blank=True)
    contract = models.ForeignKey(Contract, on_delete=models.CASCADE, related_name="payments", verbose_name="合同")
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="payments", verbose_name="客户")
    payment_stage = models.CharField("收款阶段", max_length=24, choices=PaymentStage.choices, default=PaymentStage.ADVANCE)
    amount = models.DecimalField("应收金额", max_digits=14, decimal_places=2, default=0)
    currency = models.CharField("币种", max_length=10, choices=Contract.Currency.choices, default=Contract.Currency.RMB)
    payment_date = models.DateField("收款日期", null=True, blank=True)
    payment_account = models.CharField("收款账户", max_length=160, blank=True)
    bank_fee = models.DecimalField("银行手续费", max_digits=12, decimal_places=2, default=0)
    actual_received_amount = models.DecimalField("实际收款金额", max_digits=14, decimal_places=2, default=0)
    voucher_file = models.FileField("收款凭证", upload_to="payment_vouchers/%Y/%m", blank=True, max_length=300)
    remark = models.TextField("备注", blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        ordering = ["-payment_date", "-created_at"]
        verbose_name = "到款记录"
        verbose_name_plural = "到款记录"

    def save(self, *args, **kwargs):
        if not self.payment_no:
            self.payment_no = next_monthly_no(type(self), "payment_no", "SK")
        if self.contract_id and not self.customer_id:
            self.customer = self.contract.customer
        if not self.actual_received_amount and self.amount:
            self.actual_received_amount = self.amount - (self.bank_fee or Decimal("0"))
        super().save(*args, **kwargs)

    def __str__(self):
        return self.payment_no or str(self.contract)

class VisitPlan(models.Model):
    class ArrivalStatus(models.TextChoices):
        CONFIRMED = "confirmed", "已确认"
        PENDING = "pending", "待确认"

    class Status(models.TextChoices):
        PENDING = "pending", "待确认"
        CONFIRMED = "confirmed", "已确认"
        VISITED = "visited", "已接待"
        CANCELED = "canceled", "取消"

    visit_no = models.CharField("来访编号", max_length=32, unique=True, blank=True)
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="visit_plans", verbose_name="客户")
    country_region = models.CharField("国家/地区", max_length=160, blank=True)
    visit_date = models.DateField("来访日期", null=True, blank=True, db_index=True)
    arrival_time = models.CharField("到达时间", max_length=120, blank=True)
    arrival_status = models.CharField("到达状态", max_length=20, choices=ArrivalStatus.choices, default=ArrivalStatus.PENDING)
    visit_equipment = models.CharField("参观设备", max_length=160, blank=True)
    reception_users = models.ManyToManyField(User, blank=True, related_name="reception_visits", verbose_name="接待人员")
    technician_users = models.ManyToManyField(User, blank=True, related_name="technical_visits", verbose_name="技术人员")
    need_car = models.BooleanField("是否需要车辆", default=False)
    need_demo_machine = models.BooleanField("是否需要设备展示", default=False)
    need_translator = models.BooleanField("是否需要翻译", default=False)
    status = models.CharField("状态", max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True)
    remark = models.TextField("备注", blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        ordering = ["visit_date", "arrival_time"]
        verbose_name = "客户来访"
        verbose_name_plural = "客户来访"

    def save(self, *args, **kwargs):
        if not self.visit_no:
            self.visit_no = next_monthly_no(type(self), "visit_no", "LF")
        super().save(*args, **kwargs)

    def __str__(self):
        return self.visit_no or str(self.customer)


class WorkOrderLink(models.Model):
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="work_order_links", verbose_name="客户")
    contract = models.ForeignKey(Contract, null=True, blank=True, on_delete=models.SET_NULL, related_name="work_order_links", verbose_name="合同")
    work_order_no = models.CharField("小工单编号", max_length=80, db_index=True)
    order_date = models.DateField("下单日期", null=True, blank=True)
    production_status = models.CharField("生产状态", max_length=120, blank=True)
    invoice_status = models.CharField("开票状态", max_length=120, blank=True)
    external_url = models.URLField("外部链接", blank=True)
    remark = models.TextField("备注", blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        ordering = ["-order_date", "-created_at"]
        verbose_name = "小工单关联"
        verbose_name_plural = "小工单关联"

    def __str__(self):
        return self.work_order_no


class TaskReminder(models.Model):
    class ReminderType(models.TextChoices):
        FOLLOW_UP = "follow_up", "跟进提醒"
        QUOTE = "quote", "报价提醒"
        PAYMENT = "payment", "收款提醒"
        VISIT = "visit", "来访提醒"
        LEAD = "lead", "线索处理提醒"

    class Status(models.TextChoices):
        PENDING = "pending", "待处理"
        DONE = "done", "已完成"
        OVERDUE = "overdue", "已逾期"
        CANCELED = "canceled", "已取消"

    class Priority(models.TextChoices):
        LOW = "low", "低"
        MEDIUM = "medium", "中"
        HIGH = "high", "高"

    customer = models.ForeignKey(Customer, null=True, blank=True, on_delete=models.CASCADE, related_name="task_reminders", verbose_name="客户")
    lead = models.ForeignKey(Lead, null=True, blank=True, on_delete=models.CASCADE, related_name="task_reminders", verbose_name="线索")
    quote = models.ForeignKey(Quote, null=True, blank=True, on_delete=models.CASCADE, related_name="task_reminders", verbose_name="报价")
    contract = models.ForeignKey(Contract, null=True, blank=True, on_delete=models.CASCADE, related_name="task_reminders", verbose_name="合同")
    assigned_to = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="task_reminders", verbose_name="负责人")
    reminder_type = models.CharField("提醒类型", max_length=20, choices=ReminderType.choices)
    title = models.CharField("标题", max_length=160)
    content = models.TextField("内容", blank=True)
    due_at = models.DateTimeField("截止时间", db_index=True)
    status = models.CharField("状态", max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True)
    priority = models.CharField("优先级", max_length=20, choices=Priority.choices, default=Priority.MEDIUM)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="created_task_reminders", verbose_name="创建人")
    completed_at = models.DateTimeField("完成时间", null=True, blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        ordering = ["status", "due_at"]
        verbose_name = "跟进任务"
        verbose_name_plural = "跟进任务"

    def __str__(self):
        return self.title

class Attachment(models.Model):
    class FileType(models.TextChoices):
        QUOTE = "quote", "报价单"
        CONTRACT = "contract", "合同"
        IMAGE = "image", "图片"
        AUDIO = "audio", "语音"
        VIDEO = "video", "视频"
        CHAT = "chat", "聊天截图"
        PAYMENT = "payment", "收款凭证"
        CUSTOMS = "customs", "报关单"
        PACKING = "packing", "装箱单"
        PI = "pi", "PI"
        OTHER = "other", "其他"

    customer = models.ForeignKey(Customer, null=True, blank=True, on_delete=models.CASCADE, related_name="attachments", verbose_name="客户")
    lead = models.ForeignKey(Lead, null=True, blank=True, on_delete=models.CASCADE, related_name="attachments", verbose_name="线索")
    follow_up = models.ForeignKey(ContactLog, null=True, blank=True, on_delete=models.CASCADE, related_name="attachment_files", verbose_name="跟进记录")
    quote = models.ForeignKey(Quote, null=True, blank=True, on_delete=models.CASCADE, related_name="attachments", verbose_name="报价")
    contract = models.ForeignKey(Contract, null=True, blank=True, on_delete=models.CASCADE, related_name="attachments", verbose_name="合同")
    payment = models.ForeignKey(Payment, null=True, blank=True, on_delete=models.CASCADE, related_name="attachments", verbose_name="收款")
    sample_test = models.ForeignKey(SampleTest, null=True, blank=True, on_delete=models.CASCADE, related_name="attachments", verbose_name="样罐测试")
    file = models.FileField("文件", upload_to="crm_attachments/%Y/%m", max_length=300)
    file_type = models.CharField("文件类型", max_length=20, choices=FileType.choices, default=FileType.OTHER)
    uploaded_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="uploaded_attachments", verbose_name="上传人")
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "附件"
        verbose_name_plural = "附件"

    def __str__(self):
        return self.file.name

class Reminder(models.Model):
    class ReminderType(models.TextChoices):
        NEXT_CONTACT = "next_contact", "到期跟进"
        UNCONTACTED_RULE = "uncontacted_rule", "未联系提醒"
        PROTECTED_CUSTOMER_IDLE = "protected_customer_idle", "报价/成交客户未联系提醒"
        PUBLIC_POOL_WARNING = "public_pool_warning", "公海释放提醒"
        PUBLIC_POOL_RELEASED = "public_pool_released", "已进入公海"
        QUOTE_FOLLOWUP = "quote_followup", "已报价未跟"
        PAYMENT_COLLECTION = "payment_collection", "待收款提醒"
        LEAD_FOLLOWUP = "lead_followup", "线索处理提醒"
        VISIT_PREP = "visit_prep", "来访提醒"

    class Status(models.TextChoices):
        PENDING = "pending", "待提醒"
        SENT = "sent", "已发送"
        DONE = "done", "已完成"

    customer = models.ForeignKey(Customer, null=True, blank=True, on_delete=models.CASCADE, related_name="reminders", verbose_name="客户")
    lead = models.ForeignKey(Lead, null=True, blank=True, on_delete=models.CASCADE, related_name="reminders", verbose_name="线索")
    assignee = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="reminders", verbose_name="负责人")
    reminder_type = models.CharField("提醒类型", max_length=32, choices=ReminderType.choices)
    due_at = models.DateTimeField("提醒时间", default=timezone.now)
    message = models.TextField("提醒内容")
    status = models.CharField("状态", max_length=20, choices=Status.choices, default=Status.PENDING)
    sent_at = models.DateTimeField("发送时间", null=True, blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        ordering = ["due_at"]
        verbose_name = "跟进提醒"
        verbose_name_plural = "跟进提醒"

    def __str__(self):
        return self.message[:80]


class FeishuSyncSource(models.Model):
    class SourceType(models.TextChoices):
        BASE = "base", "多维表格"
        SHEET = "sheet", "电子表格"

    class SourceKind(models.TextChoices):
        CUSTOMER = "customer", "客户/线索"
        CONTACT_LOG = "contact_log", "跟进记录"
        CONTRACT = "contract", "合同"

    name = models.CharField("来源名称", max_length=120)
    source_type = models.CharField("来源类型", max_length=20, choices=SourceType.choices, default=SourceType.BASE)
    app_token = models.CharField("应用令牌", max_length=128)
    table_id = models.CharField("表格标识", max_length=64)
    sheet_id = models.CharField("工作表标识", max_length=64, blank=True)
    sheet_range = models.CharField("工作表范围", max_length=64, blank=True)
    source_kind = models.CharField("同步内容", max_length=20, choices=SourceKind.choices, default=SourceKind.CUSTOMER)
    default_record_kind = models.CharField("默认资料类型", max_length=20, choices=Customer.RecordKind.choices, default=Customer.RecordKind.LEAD)
    field_mapping = models.JSONField("字段映射", default=dict, blank=True)
    enabled = models.BooleanField("启用", default=True)
    last_sync_at = models.DateTimeField("最近同步时间", null=True, blank=True)
    last_error = models.TextField("最近错误", blank=True)
    last_result = models.TextField("最近结果", blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        unique_together = [("app_token", "table_id", "sheet_id", "source_kind")]
        ordering = ["name"]
        verbose_name = "飞书同步源"
        verbose_name_plural = "飞书同步源"

    def __str__(self):
        return f"{self.name} / {self.get_source_kind_display()}"


class FeishuSyncRecord(models.Model):
    source = models.ForeignKey(FeishuSyncSource, on_delete=models.CASCADE, related_name="records", verbose_name="同步源")
    record_id = models.CharField("记录标识", max_length=64)
    checksum = models.CharField("校验值", max_length=64, blank=True)
    raw_fields = models.JSONField("原始字段", default=dict, blank=True)
    customer = models.ForeignKey(Customer, null=True, blank=True, on_delete=models.SET_NULL, related_name="feishu_sync_records", verbose_name="客户")
    contact_log = models.ForeignKey(ContactLog, null=True, blank=True, on_delete=models.SET_NULL, related_name="feishu_sync_records", verbose_name="跟进日志")
    contract = models.ForeignKey(Contract, null=True, blank=True, on_delete=models.SET_NULL, related_name="feishu_sync_records", verbose_name="合同")
    last_seen_at = models.DateTimeField("最近发现时间", auto_now=True)

    class Meta:
        unique_together = [("source", "record_id")]
        ordering = ["-last_seen_at"]
        verbose_name = "飞书同步记录"
        verbose_name_plural = "飞书同步记录"

    def __str__(self):
        return f"{self.source_id}:{self.record_id}"


class OperationLog(models.Model):
    class ActionType(models.TextChoices):
        CREATE = "create", "新增"
        UPDATE = "update", "编辑"
        TRANSFER = "transfer", "转移"
        MERGE = "merge", "合并"
        MARK_INVALID = "mark_invalid", "标记无效"
        RESTORE = "restore", "恢复"
        DELETE = "delete", "删除"
        CLAIM = "claim", "认领"
        LEVEL_CHANGE = "level_change", "修改级别"
        STATUS_CHANGE = "status_change", "修改状态"
        ASSIGN_LEAD = "assign_lead", "分配线索"
        PUBLIC_POOL = "public_pool", "进入公海"
        RECYCLE = "recycle", "进入回收站"
        CONTRACT = "contract", "合同变更"
        PAYMENT = "payment", "收款变更"

    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, verbose_name="操作人")
    customer = models.ForeignKey(Customer, null=True, blank=True, on_delete=models.SET_NULL, related_name="operation_logs", verbose_name="客户")
    lead = models.ForeignKey(Lead, null=True, blank=True, on_delete=models.SET_NULL, related_name="operation_logs", verbose_name="线索")
    action_type = models.CharField("动作类型", max_length=32, choices=ActionType.choices)
    before_data = models.JSONField("变更前", default=dict, blank=True)
    after_data = models.JSONField("变更后", default=dict, blank=True)
    remark = models.TextField("备注", blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "操作日志"
        verbose_name_plural = "操作日志"

    def __str__(self):
        return f"{self.get_action_type_display()} / {self.created_at:%Y-%m-%d %H:%M}"

class AuditLog(models.Model):
    actor = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, verbose_name="操作人")
    action = models.CharField("操作", max_length=80)
    target_type = models.CharField("对象类型", max_length=40)
    target_id = models.CharField("对象标识", max_length=40)
    detail = models.TextField("详情", blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "操作审计"
        verbose_name_plural = "操作审计"

    def __str__(self):
        return f"{self.action} {self.target_type}:{self.target_id}"
