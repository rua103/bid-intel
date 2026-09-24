from __future__ import annotations

import json
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import httpx

from app.config import Settings
from app.schemas import (
    ItemCandidate,
    ModelExtractionPayload,
    NoticeMetadata,
    ParticipantCandidate,
)

PROMPT = """你是政府采购公告结构化抽取器。只能从输入文本中抽取明确出现的信息，不做常识补全，不推测品牌、金额、投标资格或中标结果。
每条标的物和参与主体都填写 package_code（原文明确的采购包/标段编号；没有明确编号填 default，不凭顺序猜测）。参与主体另有 consortium_members，只有明确联合体才逐项填写原文成员名称，否则为空数组；联合体保留为一条中标记录，不向各成员重复分配整个中标金额。
标的物字段：product_name（产品/服务名称，通常对应“采购标的”）、category（品目）、brand（品牌）、model（规格型号）、quantity（数值）、quantity_unit（单位）、unit_price（单价数值）、total_price（总价数值）。
参与主体只填写公告明确列出的实际投标主体；outcome 只能是 winner（明确中标）、nonwinner（明确未中标）、unknown（身份明确但结果不明）。不要把采购人、联系人、代理机构、评审专家当成投标人。award_amount 只填公告明确给出的该主体成交金额。
金额和数量用 JSON 数值，不带千分位或货币符号；原文缺失字段设为 null。每条主体和标的物都必须附 source_evidence，复制能直接证明该条记录的原文连续片段。无法从原文证明的记录不要输出。
仅返回 JSON 对象，顶层字段为 metadata、participants、items。metadata 的键为 project_name、project_number、procurement_unit、project_budget、announced_total_award。participants 的键为 organization_name、outcome、award_amount、package_code、consortium_members、source_evidence。items 的键为 product_name、category、brand、model、quantity、quantity_unit、unit_price、total_price、package_code、source_evidence。"""


@dataclass
class ModelUsage:
    max_calls: int
    requests: int = 0
    successful_responses: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    exhausted: bool = False


_usage: ContextVar[ModelUsage | None] = ContextVar("model_usage", default=None)


@contextmanager
def model_call_budget(max_calls: int = 6):
    """Bound real HTTP requests and collect aggregate usage without storing secrets."""
    if max_calls < 0:
        raise ValueError("max_calls must be nonnegative")
    usage = ModelUsage(max_calls=max_calls)
    token = _usage.set(usage)
    try:
        yield usage
    finally:
        _usage.reset(token)


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _stream_completion(
    endpoint: str,
    api_key: str,
    body: dict[str, object],
    *,
    read_timeout: int,
    total_seconds: int,
) -> tuple[str, dict[str, object]]:
    """Read one SSE completion and return ``(content, usage)``.

    ``read_timeout`` bounds the gap *between* chunks, not the whole generation.
    A buffered response sends nothing until the model has finished, so the client
    clock used to fire first on long notices; streaming turns that wall into an
    inter-chunk idle limit. ``reasoning_content`` deltas are dropped on purpose so
    chain-of-thought never reaches the extracted JSON.
    """
    parts: list[str] = []
    usage: dict[str, object] = {}
    deadline = time.monotonic() + max(1, total_seconds)
    timeout = httpx.Timeout(connect=15, read=max(5, read_timeout), write=30, pool=15)
    with httpx.stream(
        "POST",
        endpoint,
        headers={"Authorization": f"Bearer {api_key}"},
        json=body,
        timeout=timeout,
    ) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if time.monotonic() > deadline:
                raise httpx.ReadTimeout("模型流式响应超过总时长上限")
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                chunk = json.loads(payload)
            except ValueError:
                continue
            if isinstance(chunk.get("usage"), dict):
                usage = chunk["usage"]
            for choice in chunk.get("choices") or []:
                piece = (choice.get("delta") or {}).get("content")
                if piece:
                    parts.append(piece)
    return "".join(parts), usage


def extract_unstructured_items(
    *,
    filename: str,
    text: str,
    settings: Settings,
    include_participants: bool = False,
) -> tuple[NoticeMetadata, list[ItemCandidate], list[ParticipantCandidate], list[str]]:
    if not text.strip():
        return NoticeMetadata(), [], [], []
    if not (settings.model_base_url and settings.model_api_key and settings.model_name):
        return NoticeMetadata(), [], [], [f"{filename}: 未配置模型，无法抽取非表格标的及投标主体"]
    model = settings.model_name.casefold()
    if not model.startswith(("qwen", "deepseek")):
        return (
            NoticeMetadata(),
            [],
            [],
            [f"{filename}: 模型名称不符合赛题要求的 Qwen/DeepSeek 系列，已跳过"],
        )

    base_url = settings.model_base_url.rstrip("/")
    endpoint = (
        base_url if base_url.endswith("/chat/completions") else f"{base_url}/chat/completions"
    )
    # Keep enough context for a typical notice while avoiding unbounded requests.
    excerpt = text[: max(1000, settings.model_max_chars)]
    usage = _usage.get()
    if usage is not None and usage.requests >= usage.max_calls:
        usage.exhausted = True
        return NoticeMetadata(), [], [], [f"{filename}: 实验模型调用额度已用完，未发起请求"]
    body = {
        "model": settings.model_name,
        "temperature": 0,
        "max_tokens": max(256, settings.model_max_output_tokens),
        "response_format": {"type": "json_object"},
        "stream": True,
        "stream_options": {"include_usage": True},
        "messages": [
            {"role": "system", "content": PROMPT},
            {"role": "user", "content": f"来源文件：{filename}\n公告文本：\n{excerpt}"},
        ],
    }
    try:
        if usage is not None:
            usage.requests += 1
        content, token_usage = _stream_completion(
            endpoint,
            settings.model_api_key,
            body,
            read_timeout=settings.model_timeout_seconds,
            total_seconds=settings.model_stream_total_seconds,
        )
        if usage is not None:
            usage.successful_responses += 1
            usage.prompt_tokens += int(token_usage.get("prompt_tokens") or 0)
            usage.completion_tokens += int(token_usage.get("completion_tokens") or 0)
        if not content.strip():
            return (
                NoticeMetadata(),
                [],
                [],
                [f"{filename}: 模型返回空内容（可能输出被截断或只返回了推理）"],
            )
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.IGNORECASE)
        parsed = ModelExtractionPayload.model_validate(json.loads(content))
    except (httpx.HTTPError, KeyError, IndexError, ValueError, TypeError) as exc:
        return NoticeMetadata(), [], [], [f"{filename}: 模型抽取失败（{type(exc).__name__}）"]

    verified_text = _compact(text)
    accepted: list[ItemCandidate] = []
    accepted_participants: list[ParticipantCandidate] = []
    warnings: list[str] = []
    if len(text) > len(excerpt):
        warnings.append(
            f"{filename}: 原文超过 {len(excerpt)} 字符，本次模型只读取前 {len(excerpt)} 字符"
        )
    for candidate in parsed.items:
        evidence = candidate.source_evidence.strip()
        if not evidence or _compact(evidence) not in verified_text:
            warnings.append(f"{filename}: 忽略无法在原文中定位证据的模型结果")
            continue
        if not any(
            (
                candidate.product_name,
                candidate.category,
                candidate.brand,
                candidate.model,
                candidate.total_price is not None,
            )
        ):
            continue
        accepted.append(
            ItemCandidate(
                **candidate.model_dump(exclude={"source_evidence"}),
                source_file=filename,
                source_location="model_text_evidence",
                source_evidence=evidence,
                extraction_method="qwen_deepseek_structured",
                confidence=0.55,
            )
        )

    if include_participants:
        for candidate in parsed.participants:
            evidence = candidate.source_evidence.strip()
            if (
                not candidate.organization_name.strip()
                or not evidence
                or _compact(evidence) not in verified_text
                or _compact(candidate.organization_name) not in _compact(evidence)
            ):
                warnings.append(f"{filename}: 忽略无法在原文中定位证据的主体结果")
                continue
            extra = {}
            if "package_code" in ParticipantCandidate.model_fields:
                extra["package_code"] = candidate.package_code
            if "consortium_members" in ParticipantCandidate.model_fields:
                extra["consortium_members"] = [
                    member for member in candidate.consortium_members
                    if member.strip() and _compact(member) in _compact(evidence)
                ]
                if len(extra["consortium_members"]) != len(candidate.consortium_members):
                    warnings.append(f"{filename}: 忽略证据中未出现的联合体成员")
            accepted_participants.append(
                ParticipantCandidate(
                    organization_name=candidate.organization_name.strip(),
                    outcome=candidate.outcome,
                    award_amount=candidate.award_amount,
                    source_file=filename,
                    source_location="model_text_evidence",
                    source_evidence=evidence,
                    **extra,
                )
            )
    metadata = parsed.metadata
    # Accept metadata only when its value is present in the supplied source text.
    verified_metadata: dict[str, object] = {}
    for field_name in ("project_name", "project_number", "procurement_unit"):
        value = getattr(metadata, field_name)
        if value and _compact(str(value)) in verified_text:
            verified_metadata[field_name] = value
    source_amounts: set[Decimal] = set()
    amount_text = re.sub(r"[,，\s￥¥]", "", text)
    for number in re.findall(r"-?\d+(?:\.\d+)?", amount_text):
        try:
            source_amounts.add(Decimal(number).normalize())
        except InvalidOperation:
            continue
    for field_name in ("project_budget", "announced_total_award"):
        value = getattr(metadata, field_name)
        if value is not None and value.normalize() in source_amounts:
            verified_metadata[field_name] = value
    return NoticeMetadata(**verified_metadata), accepted, accepted_participants, warnings


def test_model_connection(base_url: str, api_key: str, model_name: str) -> tuple[bool, str]:
    model = model_name.strip().casefold()
    if not model.startswith(("qwen", "deepseek")):
        return False, "模型名需以 qwen 或 deepseek 开头"
    if not (base_url.strip() and api_key.strip() and model_name.strip()):
        return False, "接口地址、API Key、模型名均不能为空"
    endpoint = base_url.rstrip("/")
    endpoint = endpoint if endpoint.endswith("/chat/completions") else f"{endpoint}/chat/completions"
    body = {
        "model": model_name.strip(),
        "temperature": 0,
        "max_tokens": 16,
        "thinking": {"type": "disabled"},
        "messages": [{"role": "user", "content": "ping"}],
    }
    try:
        response = httpx.post(
            endpoint,
            headers={"Authorization": f"Bearer {api_key.strip()}"},
            json=body,
            timeout=30,
        )
    except httpx.HTTPError as exc:
        return False, f"连接失败：{type(exc).__name__}"
    if response.status_code in (401, 403):
        return False, f"鉴权失败（HTTP {response.status_code}），请检查 API Key"
    if response.status_code >= 400:
        detail = response.text[:200].replace("\n", " ")
        return False, f"服务返回错误（HTTP {response.status_code}）：{detail}"
    return True, f"连接成功，模型 {model_name.strip()} 响应正常"
