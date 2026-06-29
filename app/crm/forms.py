from pathlib import Path
from uuid import uuid4

from django import forms
from django.contrib.auth.models import User
from django.core.files.storage import default_storage
from django.utils import timezone
from django.utils.text import get_valid_filename

from .models import (
    Contact,
    ContactLog,
    Contract,
    Customer,
    Lead,
    Opportunity,
    Payment,
    Quote,
    QuoteItem,
    QuotePlan,
    TaskReminder,
    VisitPlan,
    WorkOrderLink,
    PHONE_PREFIX_CHOICES,
    format_phone_with_prefix,
    merge_wechat_values,
    split_phone_and_wechat,
    split_phone_prefix,
)

AUDIO_FILE_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".oga", ".webm", ".amr", ".flac"}
IMAGE_FILE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic", ".heif"}


def user_display_name(user):
    return user.get_full_name() or user.get_username()


def active_user_name_choices(current=""):
    choices = [("", "请选择")]
    seen = {""}
    for user in User.objects.filter(is_active=True).order_by("username"):
        label = user_display_name(user)
        if label not in seen:
            choices.append((label, label))
            seen.add(label)
    current = str(current or "").strip()
    if current and current not in seen:
        choices.append((current, current))
    return choices


def contact_audio_upload_path(uploaded_file):
    filename = get_valid_filename(Path(uploaded_file.name or "audio").name) or "audio"
    stamp = timezone.localtime(timezone.now()).strftime("%Y%m%d%H%M%S")
    folder = timezone.localtime(timezone.now()).strftime("%Y/%m")
    return f"contact_audio/{folder}/{stamp}-{uuid4().hex[:8]}-{filename}"


from .options import (
    CUSTOMER_STATUS_OPTIONS,
    CUSTOMER_TYPE_OPTIONS,
    DEMAND_OPTIONS,
    SOURCE_OPTIONS,
    canonical_customer_statuses,
    canonical_customer_type,
    canonical_demands,
    canonical_source,
    choice_pairs,
    parse_multi_value,
)


def choices_with_current(choices, current):
    current = str(current or "").strip()
    if not current:
        return choices
    values = {value for value, _label in choices}
    if current in values:
        return choices
    return choices + [(current, current)]


def choices_with_current_multi(choices, current):
    result = list(choices)
    values = {value for value, _label in result}
    for item in parse_multi_value(current):
        if item and item not in values:
            result.append((item, item))
            values.add(item)
    return result


class DateTimeInput(forms.DateTimeInput):
    input_type = "datetime-local"

    def format_value(self, value):
        if value is None:
            return ""
        return value.strftime("%Y-%m-%dT%H:%M")


class DateInput(forms.DateInput):
    input_type = "date"

    def format_value(self, value):
        if value is None:
            return ""
        return value.strftime("%Y-%m-%d")


class MultiValueChoiceField(forms.MultipleChoiceField):
    def to_python(self, value):
        if isinstance(value, str):
            return parse_multi_value(value)
        return super().to_python(value)

class CustomerChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        number = obj.customer_no or obj.legacy_customer_no or "未编号"
        name = obj.name or obj.contact_name or "未填写客户名称"
        return f"{number} ｜ {name}"


class UserChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return user_display_name(obj)

class CustomerForm(forms.ModelForm):
    email = forms.CharField(label="邮箱", required=False, max_length=254)
    phone_prefix = forms.ChoiceField(label="电话前缀", choices=[("", "请选择")] + PHONE_PREFIX_CHOICES, required=False)
    phone_local = forms.CharField(label="客户电话", required=False, max_length=80)
    source_channel = forms.ChoiceField(label="线索来源", choices=choice_pairs(SOURCE_OPTIONS), required=False)
    customer_type = forms.ChoiceField(label="客户类型", choices=choice_pairs(CUSTOMER_TYPE_OPTIONS), required=False)
    demand = forms.MultipleChoiceField(
        label="客户需求",
        choices=choice_pairs(DEMAND_OPTIONS, include_blank=False),
        required=False,
        widget=forms.SelectMultiple(attrs={"data-dropdown-multiple": "1", "data-placeholder": "请选择客户需求"}),
    )
    customer_status_text = forms.MultipleChoiceField(
        label="客户状态",
        choices=choice_pairs(CUSTOMER_STATUS_OPTIONS, include_blank=False),
        required=False,
        widget=forms.SelectMultiple(attrs={"data-dropdown-multiple": "1", "data-placeholder": "请选择客户状态"}),
    )

    class Meta:
        model = Customer
        fields = [
            "name",
            "nickname",
            "official_name",
            "short_name",
            "company_name",
            "main_contact_name",
            "owner",
            "co_owners",
            "source_channel",
            "trade_type",
            "customer_type",
            "deal_status",
            "demand",
            "product_interest",
            "demand_summary",
            "equipment_model",
            "capacity_requirement",
            "can_type",
            "sample_can_info",
            "is_carbonated",
            "need_sample_test",
            "last_contact_at",
            "next_follow_at",
            "next_action",
            "expected_close_month",
            "grade",
            "related_lead",
            "customer_status_text",
            "status",
            "next_contact_at",
            "country_region",
            "province_city",
            "language",
            "timezone",
            "region",
            "wechat",
            "whatsapp",
            "instagram",
            "facebook",
            "platform_account",
            "phone_prefix",
            "phone_local",
            "contact_name",
            "email",
            "is_public",
            "is_recycled",
            "recycle_reason",
            "image",
            "attachment_note",
            "notes",
        ]
        widgets = {
            "last_contact_at": DateTimeInput(),
            "next_contact_at": DateInput(),
            "next_follow_at": DateTimeInput(),
            "co_owners": forms.SelectMultiple(attrs={"data-dropdown-multiple": "1", "data-placeholder": "请选择协作业务员"}),
            "product_interest": forms.Textarea(attrs={"rows": 3}),
            "demand_summary": forms.Textarea(attrs={"rows": 3}),
            "sample_can_info": forms.Textarea(attrs={"rows": 2}),
            "attachment_note": forms.Textarea(attrs={"rows": 3}),
            "notes": forms.Textarea(attrs={"rows": 4}),
        }
        labels = {
            "name": "客户名称",
            "nickname": "客户昵称/网名",
            "official_name": "客户正式名称",
            "short_name": "客户简称",
            "company_name": "公司名称",
            "main_contact_name": "主联系人姓名",
            "owner": "主业务员",
            "co_owners": "协作业务员",
            "source_channel": "线索来源",
            "trade_type": "内贸/外贸",
            "customer_type": "客户类型",
            "deal_status": "成交状态",
            "demand": "客户需求",
            "product_interest": "关注产品",
            "demand_summary": "最新需求摘要",
            "equipment_model": "设备型号",
            "capacity_requirement": "产能需求",
            "can_type": "罐型",
            "sample_can_info": "样罐信息",
            "is_carbonated": "是否含气",
            "need_sample_test": "是否需要样罐测试",
            "last_contact_at": "最后联系时间",
            "next_follow_at": "下次跟进时间",
            "next_action": "下一步动作",
            "expected_close_month": "预计成交月份",
            "grade": "客户级别",
            "related_lead": "关联线索",
            "customer_status_text": "客户状态",
            "status": "归属状态",
            "next_contact_at": "下次联系时间",
            "country_region": "国家/地区",
            "province_city": "省市地区",
            "language": "沟通语言",
            "timezone": "时区",
            "region": "地区",
            "wechat": "微信",
            "whatsapp": "WhatsApp",
            "instagram": "Instagram",
            "facebook": "Facebook",
            "platform_account": "外贸平台账号/阿里账号",
            "phone_prefix": "电话前缀",
            "phone_local": "客户电话",
            "contact_name": "联系人",
            "email": "邮箱",
            "is_public": "是否公海客户",
            "is_recycled": "是否回收站客户",
            "recycle_reason": "回收原因",
            "image": "图片",
            "attachment_note": "图片/附件说明",
            "notes": "沟通记录/备注",
        }
        help_texts = {
            "phone_local": "填写电话时必须选择前缀；保存格式统一为 +区号 空格 号码。",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        instance = kwargs.get("instance")
        if instance:
            self.initial["demand"] = parse_multi_value(instance.demand)
            self.initial["customer_status_text"] = parse_multi_value(instance.customer_status_text)
            phone_prefix, phone_local = split_phone_prefix(instance.phone)
            self.initial["phone_prefix"] = phone_prefix or "+86"
            self.initial["phone_local"] = phone_local
        else:
            self.initial.setdefault("phone_prefix", "+86")
        for field_name, base_choices in {
            "source_channel": choice_pairs(SOURCE_OPTIONS),
            "customer_type": choice_pairs(CUSTOMER_TYPE_OPTIONS),
        }.items():
            current = self.initial.get(field_name)
            if instance:
                current = getattr(instance, field_name, current)
            self.fields[field_name].choices = choices_with_current(base_choices, current)
        self.fields["demand"].choices = choices_with_current_multi(choice_pairs(DEMAND_OPTIONS, include_blank=False), self.initial.get("demand"))
        self.fields["customer_status_text"].choices = choices_with_current_multi(
            choice_pairs(CUSTOMER_STATUS_OPTIONS, include_blank=False),
            self.initial.get("customer_status_text"),
        )
        if "owner" in self.fields:
            self.fields["owner"].queryset = User.objects.filter(is_active=True).order_by("username")
        if "co_owners" in self.fields:
            self.fields["co_owners"].queryset = User.objects.filter(is_active=True).order_by("username")

    def clean_source_channel(self):
        return canonical_source(self.cleaned_data.get("source_channel"))

    def clean_customer_type(self):
        return canonical_customer_type(self.cleaned_data.get("customer_type"))

    def clean_demand(self):
        return canonical_demands(self.cleaned_data.get("demand"))

    def clean_customer_status_text(self):
        return canonical_customer_statuses(self.cleaned_data.get("customer_status_text"))

    def clean(self):
        cleaned = super().clean()
        prefix = cleaned.get("phone_prefix")
        phone_local = str(cleaned.get("phone_local") or "").strip()
        if phone_local and not prefix:
            phone, phone_wechat = split_phone_and_wechat(phone_local, cleaned.get("region"), cleaned.get("name"))
            if phone:
                self.add_error("phone_prefix", "填写客户电话时必须选择电话前缀。")
            else:
                cleaned["wechat"] = merge_wechat_values(cleaned.get("wechat"), phone_wechat)
                cleaned["phone"] = ""
                return cleaned
        cleaned["phone"] = format_phone_with_prefix(prefix, phone_local)
        return cleaned

    def save(self, commit=True):
        customer = super().save(commit=False)
        customer.phone = self.cleaned_data.get("phone", "")
        customer.wechat = self.cleaned_data.get("wechat", "")
        if commit:
            customer.save()
            self.save_m2m()
        return customer


class ContactLogForm(forms.ModelForm):
    follower_name = forms.ChoiceField(label="跟进人", required=False)
    level_after = forms.ChoiceField(label="沟通后客户级别", required=False)
    status_after = forms.ChoiceField(label="沟通后客户状态", required=False)
    result = MultiValueChoiceField(
        label="跟进结果",
        choices=choice_pairs(CUSTOMER_STATUS_OPTIONS, include_blank=False),
        required=False,
        widget=forms.SelectMultiple(attrs={"data-dropdown-multiple": "1", "data-placeholder": "请选择跟进结果"}),
    )
    photo_file = forms.FileField(
        label="跟进内容照片",
        required=False,
        widget=forms.ClearableFileInput(attrs={"accept": "image/*"}),
    )
    audio_file = forms.FileField(
        label="音频文件",
        required=False,
        widget=forms.ClearableFileInput(attrs={"accept": "audio/*"}),
    )

    class Meta:
        model = ContactLog
        fields = [
            "contact_at", "method", "channel", "contact_person", "source", "followed_by", "follower_name",
            "summary", "content", "demand_update", "quote_update", "sample_update", "customer_feedback",
            "next_action", "result", "level_after", "status_after", "photo_file", "photo_note", "attachments",
            "audio_file", "next_contact_at",
        ]
        widgets = {
            "contact_at": DateTimeInput(),
            "next_contact_at": DateInput(),
            "summary": forms.Textarea(attrs={"rows": 4}),
            "content": forms.Textarea(attrs={"rows": 4}),
            "demand_update": forms.Textarea(attrs={"rows": 3}),
            "quote_update": forms.Textarea(attrs={"rows": 2}),
            "sample_update": forms.Textarea(attrs={"rows": 2}),
            "customer_feedback": forms.Textarea(attrs={"rows": 2}),
            "attachments": forms.Textarea(attrs={"rows": 2}),
            "photo_note": forms.Textarea(attrs={"rows": 2}),
        }
        labels = {
            "contact_at": "跟进时间",
            "method": "跟进形式",
            "channel": "沟通渠道",
            "contact_person": "沟通联系人",
            "source": "录入来源",
            "followed_by": "跟进人账号",
            "follower_name": "跟进人",
            "summary": "跟进内容",
            "content": "沟通内容",
            "demand_update": "需求更新",
            "quote_update": "报价情况",
            "sample_update": "样罐情况",
            "customer_feedback": "客户反馈",
            "next_action": "下一步动作",
            "result": "跟进结果",
            "level_after": "沟通后客户级别",
            "status_after": "沟通后客户状态",
            "photo_file": "跟进内容照片",
            "photo_note": "照片说明",
            "attachments": "附件说明",
            "audio_file": "音频文件",
            "next_contact_at": "下次联系时间",
        }
        help_texts = {
            "photo_file": "支持 jpg、png、gif、webp、heic 等图片文件。",
            "audio_file": "支持 mp3、wav、m4a、aac、ogg、webm、amr、flac 等音频文件。",
            "next_contact_at": "不填时，系统会按客户级别自动计算。",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        current = self.initial.get("follower_name")
        current_result = self.initial.get("result")
        current_level_after = self.initial.get("level_after")
        current_status_after = self.initial.get("status_after")
        if self.instance and self.instance.pk:
            current = self.instance.follower_name or current
            current_result = parse_multi_value(self.instance.result)
            current_level_after = self.instance.level_after or current_level_after
            current_status_after = self.instance.status_after or current_status_after
            self.initial["result"] = current_result
            self.initial["status_after"] = current_status_after
        if self.is_bound:
            if hasattr(self.data, "getlist"):
                current_result = self.data.getlist("result") or self.data.get("result", "")
            else:
                current_result = self.data.get("result", "")
            current_level_after = self.data.get("level_after", current_level_after)
            current_status_after = self.data.get("status_after", current_status_after)
        self.fields["follower_name"].choices = active_user_name_choices(current)
        self.fields["level_after"].choices = choices_with_current([("", "---------")] + list(Customer.Grade.choices), current_level_after)
        self.fields["result"].choices = choices_with_current_multi(
            choice_pairs(CUSTOMER_STATUS_OPTIONS, include_blank=False),
            current_result,
        )
        self.fields["status_after"].choices = choices_with_current(choice_pairs(CUSTOMER_STATUS_OPTIONS), current_status_after)

    def clean_result(self):
        return canonical_customer_statuses(self.cleaned_data.get("result"))

    def clean_status_after(self):
        return canonical_customer_statuses(self.cleaned_data.get("status_after"))

    def clean_photo_file(self):
        uploaded_file = self.cleaned_data.get("photo_file")
        if not uploaded_file:
            return uploaded_file
        extension = Path(uploaded_file.name or "").suffix.lower()
        content_type = str(getattr(uploaded_file, "content_type", "") or "").lower()
        if extension not in IMAGE_FILE_EXTENSIONS and not content_type.startswith("image/"):
            raise forms.ValidationError("请上传图片文件。")
        return uploaded_file

    def clean_audio_file(self):
        uploaded_file = self.cleaned_data.get("audio_file")
        if not uploaded_file:
            return uploaded_file
        extension = Path(uploaded_file.name or "").suffix.lower()
        content_type = str(getattr(uploaded_file, "content_type", "") or "").lower()
        if extension not in AUDIO_FILE_EXTENSIONS and not content_type.startswith("audio/"):
            raise forms.ValidationError("请上传音频文件。")
        return uploaded_file

    def save(self, commit=True):
        log = super().save(commit=False)
        uploaded_file = self.cleaned_data.get("audio_file")
        if uploaded_file:
            log.minutes_link = default_storage.save(contact_audio_upload_path(uploaded_file), uploaded_file)
        if commit:
            log.save()
        return log


class ContactLogCreateForm(ContactLogForm):
    class Meta(ContactLogForm.Meta):
        fields = ["customer"] + ContactLogForm.Meta.fields
        labels = dict(ContactLogForm.Meta.labels, customer="客户名称")


class ContractForm(forms.ModelForm):
    customer = CustomerChoiceField(queryset=Customer.objects.none(), label="客户名称")
    signed_by = UserChoiceField(queryset=User.objects.filter(is_active=True).order_by("username"), label="签约人员", required=False)
    sales_user = UserChoiceField(queryset=User.objects.filter(is_active=True).order_by("username"), label="销售负责人", required=False)

    class Meta:
        model = Contract
        fields = [
            "customer", "quote", "opportunity", "signed_by", "sales_user", "signed_date", "signed_at",
            "amount", "contract_amount", "currency", "payment_terms", "advance_payment_ratio",
            "advance_payment_amount", "status", "has_work_order", "work_order_no", "contract_file",
            "attachment_file", "attachment_note", "remark",
        ]
        widgets = {
            "signed_date": DateInput(),
            "signed_at": DateInput(),
            "payment_terms": forms.Textarea(attrs={"rows": 3}),
            "remark": forms.Textarea(attrs={"rows": 3}),
            "contract_file": forms.ClearableFileInput(attrs={"accept": ".pdf,.doc,.docx,.xls,.xlsx,.jpg,.jpeg,.png,.webp,.zip,.rar"}),
            "attachment_file": forms.ClearableFileInput(attrs={"accept": ".pdf,.doc,.docx,.xls,.xlsx,.jpg,.jpeg,.png,.webp,.zip,.rar"}),
            "attachment_note": forms.Textarea(attrs={"rows": 3}),
        }
        labels = {
            "customer": "客户名称",
            "quote": "关联报价",
            "opportunity": "关联商机",
            "signed_by": "签约人员",
            "sales_user": "销售负责人",
            "signed_date": "签约日期",
            "signed_at": "签订日期",
            "amount": "旧合同金额",
            "contract_amount": "合同金额",
            "currency": "币种",
            "payment_terms": "付款方式",
            "advance_payment_ratio": "预付款比例",
            "advance_payment_amount": "预付款金额",
            "status": "合同状态",
            "has_work_order": "是否已生成小工单",
            "work_order_no": "小工单编号",
            "contract_file": "合同文件",
            "attachment_file": "合同附件",
            "attachment_note": "附件说明",
            "remark": "备注",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["customer"].required = True
        user_qs = User.objects.filter(is_active=True).order_by("username")
        self.fields["signed_by"].queryset = user_qs
        self.fields["sales_user"].queryset = user_qs
        self.fields["quote"].queryset = Quote.objects.select_related("customer").order_by("-quote_date", "-created_at")[:2000]
        self.fields["opportunity"].queryset = Opportunity.objects.select_related("customer").order_by("-updated_at")[:2000]

    def save(self, commit=True):
        contract = super().save(commit=False)
        if contract.customer:
            contract.customer_name = str(contract.customer)
        if contract.signed_by:
            contract.signed_by_name = user_display_name(contract.signed_by)
        if commit:
            contract.save()
        return contract


class ContactForm(forms.ModelForm):
    class Meta:
        model = Contact
        fields = ["customer", "name", "position", "phone", "wechat", "whatsapp", "email", "language", "is_primary", "remark"]
        widgets = {"remark": forms.Textarea(attrs={"rows": 3})}
        labels = {"customer": "客户", "name": "联系人", "position": "职位", "phone": "电话", "wechat": "微信", "whatsapp": "WhatsApp", "email": "邮箱", "language": "语言", "is_primary": "主联系人", "remark": "备注"}

    def __init__(self, *args, **kwargs):
        customer_qs = kwargs.pop("customer_queryset", None)
        super().__init__(*args, **kwargs)
        if customer_qs is not None:
            self.fields["customer"].queryset = customer_qs


class LeadForm(forms.ModelForm):
    class Meta:
        model = Lead
        fields = [
            "raw_nickname", "customer_name", "contact_name", "phone", "wechat", "whatsapp", "email",
            "instagram", "facebook", "country_region", "language", "trade_type", "source_channel",
            "customer_type", "product_demand", "equipment_model", "capacity_requirement", "can_type",
            "sample_can_info", "is_carbonated", "status", "owner", "co_owners", "next_contact_at", "notes",
        ]
        widgets = {
            "co_owners": forms.SelectMultiple(attrs={"data-dropdown-multiple": "1", "data-placeholder": "请选择协作负责人"}),
            "next_contact_at": DateTimeInput(),
            "product_demand": forms.Textarea(attrs={"rows": 3}),
            "sample_can_info": forms.Textarea(attrs={"rows": 2}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }
        labels = {
            "raw_nickname": "原始昵称/网名", "customer_name": "客户名称", "contact_name": "联系人", "phone": "电话", "wechat": "微信", "whatsapp": "WhatsApp", "email": "邮箱", "instagram": "Instagram", "facebook": "Facebook", "country_region": "国家/地区", "language": "沟通语言", "trade_type": "内贸/外贸", "source_channel": "线索来源", "customer_type": "客户类型", "product_demand": "产品需求", "equipment_model": "设备型号", "capacity_requirement": "产能", "can_type": "罐型", "sample_can_info": "样罐", "is_carbonated": "是否含气", "status": "线索状态", "owner": "主负责人", "co_owners": "协作负责人", "next_contact_at": "下次联系时间", "notes": "备注"
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        user_qs = User.objects.filter(is_active=True).order_by("username")
        self.fields["owner"].queryset = user_qs
        self.fields["co_owners"].queryset = user_qs

    def save(self, commit=True):
        lead = super().save(commit=False)
        if lead.owner_id and not lead.assigned_at:
            lead.assigned_at = timezone.now()
        if lead.owner_id and lead.status in {Lead.Status.NEW, Lead.Status.PENDING_ASSIGN}:
            lead.status = Lead.Status.ASSIGNED
        if commit:
            lead.save()
            self.save_m2m()
        return lead


class OpportunityForm(forms.ModelForm):
    class Meta:
        model = Opportunity
        fields = ["customer", "owner", "stage", "expected_amount", "currency", "expected_close_month", "probability", "source_channel", "product_interest", "latest_progress", "next_action", "next_follow_at", "status"]
        widgets = {"next_follow_at": DateTimeInput(), "product_interest": forms.Textarea(attrs={"rows": 2}), "latest_progress": forms.Textarea(attrs={"rows": 3})}
        labels = {"customer": "客户", "owner": "负责人", "stage": "阶段", "expected_amount": "预计金额", "currency": "币种", "expected_close_month": "预计成交月份", "probability": "成交概率", "source_channel": "来源渠道", "product_interest": "关注产品", "latest_progress": "最新进展", "next_action": "下一步动作", "next_follow_at": "下次跟进", "status": "状态"}

    def __init__(self, *args, **kwargs):
        customer_qs = kwargs.pop("customer_queryset", None)
        super().__init__(*args, **kwargs)
        if customer_qs is not None:
            self.fields["customer"].queryset = customer_qs
        self.fields["owner"].queryset = User.objects.filter(is_active=True).order_by("username")


class QuoteForm(forms.ModelForm):
    class Meta:
        model = Quote
        fields = ["customer", "lead", "opportunity", "quote_date", "quoted_by", "status", "currency", "total_amount", "valid_until", "attachment", "remark"]
        widgets = {"quote_date": DateInput(), "valid_until": DateInput(), "remark": forms.Textarea(attrs={"rows": 3}), "attachment": forms.ClearableFileInput(attrs={"accept": ".pdf,.doc,.docx,.xls,.xlsx,.jpg,.jpeg,.png,.webp,.zip,.rar"})}
        labels = {"customer": "客户", "lead": "关联线索", "opportunity": "关联商机", "quote_date": "报价日期", "quoted_by": "报价人", "status": "状态", "currency": "币种", "total_amount": "报价总额", "valid_until": "有效期至", "attachment": "报价单附件", "remark": "备注"}

    def __init__(self, *args, **kwargs):
        customer_qs = kwargs.pop("customer_queryset", None)
        super().__init__(*args, **kwargs)
        if customer_qs is not None:
            self.fields["customer"].queryset = customer_qs
        self.fields["quoted_by"].queryset = User.objects.filter(is_active=True).order_by("username")
        self.fields["lead"].queryset = Lead.objects.filter(is_active=True).order_by("-created_at")[:2000]
        self.fields["opportunity"].queryset = Opportunity.objects.order_by("-updated_at")[:2000]


class QuotePlanForm(forms.ModelForm):
    class Meta:
        model = QuotePlan
        fields = ["quote", "plan_name", "equipment_model", "capacity", "main_machine_config", "can_type", "is_carbonated", "price", "quantity", "remark"]
        widgets = {"main_machine_config": forms.Textarea(attrs={"rows": 3}), "remark": forms.Textarea(attrs={"rows": 2})}
        labels = {"quote": "报价", "plan_name": "方案名称", "equipment_model": "设备型号", "capacity": "产能", "main_machine_config": "主机配置", "can_type": "罐型", "is_carbonated": "是否含气", "price": "单价", "quantity": "数量", "remark": "备注"}


class QuoteItemForm(forms.ModelForm):
    class Meta:
        model = QuoteItem
        fields = ["quote_plan", "item_type", "item_name", "specification", "quantity", "unit_price", "remark"]
        labels = {"quote_plan": "报价方案", "item_type": "明细类型", "item_name": "名称", "specification": "规格", "quantity": "数量", "unit_price": "单价", "remark": "备注"}


class PaymentForm(forms.ModelForm):
    class Meta:
        model = Payment
        fields = ["contract", "customer", "payment_stage", "amount", "currency", "payment_date", "payment_account", "bank_fee", "actual_received_amount", "voucher_file", "remark"]
        widgets = {"payment_date": DateInput(), "remark": forms.Textarea(attrs={"rows": 3}), "voucher_file": forms.ClearableFileInput(attrs={"accept": ".pdf,.jpg,.jpeg,.png,.webp,.xls,.xlsx,.doc,.docx"})}
        labels = {"contract": "合同", "customer": "客户", "payment_stage": "收款阶段", "amount": "应收金额", "currency": "币种", "payment_date": "收款日期", "payment_account": "收款账户", "bank_fee": "银行手续费", "actual_received_amount": "实际收款金额", "voucher_file": "收款凭证", "remark": "备注"}

    def __init__(self, *args, **kwargs):
        contract_qs = kwargs.pop("contract_queryset", None)
        customer_qs = kwargs.pop("customer_queryset", None)
        super().__init__(*args, **kwargs)
        if contract_qs is not None:
            self.fields["contract"].queryset = contract_qs
        if customer_qs is not None:
            self.fields["customer"].queryset = customer_qs


class VisitPlanForm(forms.ModelForm):
    class Meta:
        model = VisitPlan
        fields = ["customer", "country_region", "visit_date", "arrival_time", "arrival_status", "visit_equipment", "reception_users", "technician_users", "need_car", "need_demo_machine", "need_translator", "status", "remark"]
        widgets = {"visit_date": DateInput(), "reception_users": forms.SelectMultiple(attrs={"data-dropdown-multiple": "1", "data-placeholder": "请选择接待人员"}), "technician_users": forms.SelectMultiple(attrs={"data-dropdown-multiple": "1", "data-placeholder": "请选择技术人员"}), "remark": forms.Textarea(attrs={"rows": 3})}
        labels = {"customer": "客户", "country_region": "国家/地区", "visit_date": "来访日期", "arrival_time": "到达时间", "arrival_status": "到达状态", "visit_equipment": "参观设备", "reception_users": "接待人员", "technician_users": "技术人员", "need_car": "需要车辆", "need_demo_machine": "需要设备展示", "need_translator": "需要翻译", "status": "状态", "remark": "备注"}

    def __init__(self, *args, **kwargs):
        customer_qs = kwargs.pop("customer_queryset", None)
        super().__init__(*args, **kwargs)
        if customer_qs is not None:
            self.fields["customer"].queryset = customer_qs
        user_qs = User.objects.filter(is_active=True).order_by("username")
        self.fields["reception_users"].queryset = user_qs
        self.fields["technician_users"].queryset = user_qs


class TaskReminderForm(forms.ModelForm):
    class Meta:
        model = TaskReminder
        fields = ["customer", "lead", "quote", "contract", "assigned_to", "reminder_type", "title", "content", "due_at", "status", "priority"]
        widgets = {"due_at": DateTimeInput(), "content": forms.Textarea(attrs={"rows": 3})}
        labels = {"customer": "客户", "lead": "线索", "quote": "报价", "contract": "合同", "assigned_to": "负责人", "reminder_type": "提醒类型", "title": "标题", "content": "内容", "due_at": "截止时间", "status": "状态", "priority": "优先级"}


class WorkOrderLinkForm(forms.ModelForm):
    class Meta:
        model = WorkOrderLink
        fields = ["customer", "contract", "work_order_no", "order_date", "production_status", "invoice_status", "external_url", "remark"]
        widgets = {"order_date": DateInput(), "remark": forms.Textarea(attrs={"rows": 3})}
        labels = {"customer": "客户", "contract": "合同", "work_order_no": "小工单编号", "order_date": "下单日期", "production_status": "生产状态", "invoice_status": "开票状态", "external_url": "外部链接", "remark": "备注"}


class CustomerTransferForm(forms.Form):
    new_owner = UserChoiceField(queryset=User.objects.filter(is_active=True).order_by("username"), label="新负责人")
    reason = forms.CharField(label="转移原因", required=False, widget=forms.Textarea(attrs={"rows": 3}))


class CustomerMergeForm(forms.Form):
    target_customer = CustomerChoiceField(queryset=Customer.objects.none(), label="主客户")
    reason = forms.CharField(label="合并原因", required=False, widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args, **kwargs):
        customer_queryset = kwargs.pop("customer_queryset", Customer.objects.none())
        exclude_customer = kwargs.pop("exclude_customer", None)
        super().__init__(*args, **kwargs)
        if exclude_customer is not None:
            customer_queryset = customer_queryset.exclude(pk=exclude_customer.pk)
        self.fields["target_customer"].queryset = customer_queryset