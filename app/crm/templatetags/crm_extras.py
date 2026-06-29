import re

from django import template

from crm.options import canonical_customer_statuses, parse_multi_value


register = template.Library()


@register.filter
def split_csv(value):
    return [item.strip() for item in re.split(r"[,，、;；\n]+", str(value or "")) if item.strip()]



OWNERSHIP_STATUS_LABELS = {"私有客户", "公海客户", "成交客户", "回收站客户", "回收站"}


@register.filter
def business_status_tags(value):
    canonical = canonical_customer_statuses(value)
    items = parse_multi_value(canonical) if canonical else []
    if not items:
        items = parse_multi_value(value)
    return [item for item in items if item and item not in OWNERSHIP_STATUS_LABELS]


@register.filter
def grade_tag_class(value):
    mapping = {
        "重点客户": "tag-key",
        "意向客户": "tag-intention",
        "一般客户": "tag-normal",
        "潜在客户": "tag-potential",
        "待孵化客户": "tag-incubating",
        "待定客户": "tag-uncertain",
        "无效客户": "tag-invalid",
    }
    return mapping.get(str(value or ""), "tag-muted")


@register.filter
def status_tag_class(value):
    mapping = {
        "未报价": "tag-danger",
        "报价中": "tag-warning",
        "已报价": "tag-success",
        "待拜访": "tag-warning",
        "方案设计沟通中": "tag-info",
        "已加联系方式": "tag-contacted",
        "微信未通过": "tag-muted",
        "未加联系方式": "tag-muted",
        "已下单": "tag-key",
        "合同已签待预付": "tag-key",
    }
    return mapping.get(str(value or ""), "tag-muted")
