"""Conservative, deterministic reconciliation of independently sourced item rows.

This module never infers a package from the other packages present in a notice.
Callers must supply per-document context obtained by their extraction route; a
notice-wide metadata object must not be copied into every source's context.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.package_codes import DEFAULT_PACKAGE_CODE, normalize_package_code
from app.schemas import ItemCandidate, NoticeMetadata

_FIELDS = ("category", "brand", "model", "quantity", "quantity_unit", "unit_price", "total_price")
_PROVENANCE = "candidate_reconciliation_v1"
_CODE = r"[A-Za-z0-9]+(?:[-_/][A-Za-z0-9]+)*"
_LABEL = r"(?:采购包编号|合同包编号|标包编号|包编号|包号|合同包|采购包|标包|包)"
_ALIAS_RE = re.compile(
    rf"{_LABEL}\s*[:：]?\s*({_CODE})\s*[（(]\s*"
    rf"(?:{_LABEL}\s*[:：]?\s*)?({_CODE})\s*[）)]", re.IGNORECASE,
)
_PACKAGE_RE = re.compile(rf"{_LABEL}\s*[:：]?\s*[（(]?\s*({_CODE})", re.IGNORECASE)
_MULTI_PACKAGE_RE = re.compile(
    rf"{_LABEL}\s*[:：]?\s*{_CODE}\s*[,，、]\s*{_CODE}", re.IGNORECASE,
)
_GENERIC_MODELS = {"定制", "自制", "定做", "定制款", "通用", "无", "不适用", "详见附件", "标准型"}


def _key(value: object) -> str:
    return "".join(unicodedata.normalize("NFKC", str(value or "")).split()).casefold()


@dataclass(frozen=True)
class PackageAlias:
    """Two package codes explicitly equated in one source excerpt.

    Only a labelled parenthetical equivalence is accepted, and the excerpt must
    occur in ``source_texts[source_file]``. Co-occurrence or a shared suffix does
    not establish an alias.
    """

    source_file: str
    left: str
    right: str
    evidence: str


def discover_package_aliases(source_texts: Mapping[str, str]) -> list[PackageAlias]:
    """Read explicit ``包号:A（采购包编号:LONG-A）`` style equivalences."""
    return [
        PackageAlias(filename, match[1], match[2], match[0])
        for filename, text in sorted(source_texts.items())
        for match in _ALIAS_RE.finditer(unicodedata.normalize("NFKC", text))
        if _key(match[1]) != _key(match[2])
    ]


def _origins(row: ItemCandidate) -> list[dict]:
    try:
        evidence = json.loads(row.source_evidence or "")
    except (ValueError, TypeError):
        evidence = None
    if isinstance(evidence, dict) and evidence.get("format") == _PROVENANCE:
        sources = evidence.get("sources")
        if (isinstance(sources, list) and sources
                and all(isinstance(source, dict) and "source_file" in source
                        and "source_location" in source for source in sources)):
            return sources
    return [row.model_dump(mode="json")]


def _origin_files(row: ItemCandidate) -> set[str]:
    return {origin["source_file"] for origin in _origins(row)}


def _specific_name_supported(row: ItemCandidate) -> bool:
    """Check a quoted numbered detail row places its name before its model."""
    name, model = _key(row.product_name), _key(row.model)
    if not name or not model:
        return False
    for origin in _origins(row):
        text = origin.get("source_evidence") or ""
        if not re.match(r"^\s*\d+(?:-\d+)*[.、]?\s+", text):
            continue
        evidence = _key(text)
        name_position, model_position = evidence.find(name), evidence.find(model)
        if 0 <= name_position < 40 and model_position >= name_position + len(name):
            return True
    return False


def _sort_key(row: ItemCandidate) -> tuple:
    # Choose the notice as the stable anchor, preserving its reported item name.
    html = row.source_file.lower().endswith((".html", ".htm"))
    location = tuple(int(part) if part.isdigit() else part
                     for part in re.split(r"(\d+)", row.source_location))
    return (not html, row.source_file, location,
            json.dumps(row.model_dump(mode="json"), ensure_ascii=False, sort_keys=True))


def _supported_package(row: ItemCandidate, code: str, source_texts: Mapping[str, str]) -> bool:
    """Require package evidence local to a row or explicit document label.

    A hierarchical item id is recognized only at the start of quoted row text.
    A filename package label is useful evidence, but an archive's existence is
    never evidence that its contents belong to a particular package.
    """
    for origin in _origins(row):
        leaf = origin["source_file"].replace("\\", "/").rsplit("/", 1)[-1]
        for text in (origin.get("source_evidence") or "", leaf,
                     source_texts.get(origin["source_file"], "")):
            if _MULTI_PACKAGE_RE.search(unicodedata.normalize("NFKC", text)):
                continue
            labelled_codes = {_key(normalize_package_code(match[1]))
                              for match in _PACKAGE_RE.finditer(
                                  unicodedata.normalize("NFKC", text))}
            if labelled_codes == {_key(code)}:
                return True
            if any(_key(code) in {_key(match[1]), _key(match[2])}
                   and labelled_codes <= {_key(match[1]), _key(match[2])}
                   for match in _ALIAS_RE.finditer(unicodedata.normalize("NFKC", text))):
                return True
        hierarchical = re.match(r"^\s*(\d+)-\d+(?:-\d+)?(?:\s|\t|\|)",
                                origin.get("source_evidence") or "")
        if hierarchical and _key(hierarchical[1]) == _key(code):
            return True
    return False


def reconcile_candidates(
    items: Sequence[ItemCandidate], warnings: list[str] | None = None, *,
    metadata: NoticeMetadata | None = None,
    source_texts: Mapping[str, str] | None = None,
    source_metadata: Mapping[str, NoticeMetadata] | None = None,
    source_links: Sequence[tuple[str, str]] = (),
    package_aliases: Sequence[PackageAlias] = (),
) -> list[ItemCandidate]:
    """Fuse only compatible, uniquely corresponding cross-document rows.

    Source correspondence requires an explicit supplied link, an exact filename
    reference, or independently extracted equal project numbers. Matching needs
    equal model+quantity, or quantity/model together with a price. All populated
    semantic/numeric fields must agree. Names may differ only when one equals an
    independently extracted project name and distinctive model+quantity also agree.
    ``metadata`` is accepted for caller compatibility; only source_metadata can
    establish a project's generic name for a particular document.

    Candidate components must be cliques with one original row per source file.
    Many-to-many ambiguity, conflicts, and unsupported package assignment retain
    every row. Provenance is stored as JSON in the existing source_evidence field,
    including the complete original candidates and the decisions' source context.
    No API, Gold, historical-material filter, or schema extension is involved.
    """
    messages: set[str] = set()
    source_texts = source_texts or {}
    source_metadata = source_metadata or {}
    links = {frozenset(pair) for pair in source_links if len(set(pair)) == 2}
    filenames = {filename for row in items for filename in _origin_files(row)}
    for filename, text in source_texts.items():
        for other in filenames - {filename}:
            # Full leaf references, not a shared ZIP prefix or a guessed stem.
            leaf = other.replace("\\", "/").rsplit("/", 1)[-1].split("!/")[-1]
            if len(leaf) >= 6 and leaf in text:
                links.add(frozenset((filename, other)))

    aliases: dict[tuple[str, str], set[str]] = {}
    alias_evidence: dict[str, list[dict]] = {}
    for alias in (*discover_package_aliases(source_texts), *package_aliases):
        text = source_texts.get(alias.source_file, "")
        evidence = unicodedata.normalize("NFKC", alias.evidence)
        left, right = normalize_package_code(alias.left), normalize_package_code(alias.right)
        valid = (evidence in unicodedata.normalize("NFKC", text)
                 and any({_key(match[1]), _key(match[2])} == {_key(left), _key(right)}
                         for match in _ALIAS_RE.finditer(evidence)))
        if not valid or DEFAULT_PACKAGE_CODE in (left, right):
            messages.add(f"包号别名缺少明确原文对应，保留原包号：{alias.source_file}")
            continue
        canonical = min((left, right), key=lambda code: (len(code), _key(code), code))
        for code in (left, right):
            aliases.setdefault((alias.source_file, _key(code)), set()).add(canonical)
        alias_evidence.setdefault(alias.source_file, []).append({
            "left": left, "right": right, "evidence": alias.evidence,
        })

    def package(row: ItemCandidate) -> str:
        code = normalize_package_code(row.package_code)
        values = aliases.get((row.source_file, _key(code)), set())
        if len(values) > 1:
            messages.add(f"包号别名对应歧义，保留原包号：{row.source_file} / {code}")
            return code
        return next(iter(values)) if values else code

    def sources_correspond(left: ItemCandidate, right: ItemCandidate) -> bool:
        for a in _origin_files(left):
            for b in _origin_files(right):
                if frozenset((a, b)) in links:
                    return True
                a_meta, b_meta = source_metadata.get(a), source_metadata.get(b)
                if (a_meta and b_meta and _key(a_meta.project_number)
                        and _key(a_meta.project_number) == _key(b_meta.project_number)):
                    return True
        return False

    def generic_name(row: ItemCandidate) -> bool:
        return any(_key(row.product_name) == _key(source_metadata[filename].project_name)
                   for filename in _origin_files(row)
                   if filename in source_metadata and source_metadata[filename].project_name)

    rows = sorted(items, key=_sort_key)
    edges: list[set[int]] = [set() for _ in rows]
    for i, left in enumerate(rows):
        for j in range(i + 1, len(rows)):
            right = rows[j]
            if _origin_files(left) & _origin_files(right) or not sources_correspond(left, right):
                continue
            names = (_key(left.product_name), _key(right.product_name))
            if not all(names):
                continue
            generic = names[0] != names[1] and (generic_name(left) or generic_name(right))
            if names[0] != names[1] and not generic:
                continue
            shared, conflicts = set(), []
            for field in _FIELDS:
                a, b = getattr(left, field), getattr(right, field)
                if a is None or b is None or a == "" or b == "":
                    continue
                if (_key(a) == _key(b)) if isinstance(a, str) else a == b:
                    shared.add(field)
                else:
                    conflicts.append(field)
            distinctive_model = (_key(left.model) not in {_key(m) for m in _GENERIC_MODELS}
                                 and _key(left.model) not in {
                                     _key(left.brand), _key(right.brand), ""})
            strong = ({"model", "quantity"} <= shared and distinctive_model or
                      bool(shared & {"unit_price", "total_price"})
                      and bool(shared & {"quantity", "model"}))
            if generic:
                strong = {"model", "quantity"} <= shared and distinctive_model
            if not strong:
                continue
            codes = (package(left), package(right))
            if codes[0] != codes[1] and DEFAULT_PACKAGE_CODE not in codes:
                messages.add(f"跨文件候选包号冲突，保留待核验：{left.product_name} / "
                             f"{codes[0]},{codes[1]}")
                continue
            if conflicts:
                messages.add(f"跨文件候选字段冲突，保留待核验：{left.product_name} / "
                             + ",".join(conflicts))
                continue
            if (codes[0] == codes[1] != DEFAULT_PACKAGE_CODE
                    and not any(_supported_package(candidate, candidate.package_code, source_texts)
                                for candidate in (left, right))):
                messages.add(f"跨文件包号缺少明确原文证据，保留待核验：{left.product_name}")
                continue
            if codes[0] != codes[1]:
                known = left if codes[0] != DEFAULT_PACKAGE_CODE else right
                known_code = normalize_package_code(known.package_code)
                if not _supported_package(known, known_code, source_texts):
                    messages.add(f"跨文件缺失包号无法获得明确证据，保留待核验：{left.product_name}")
                    continue
            edges[i].add(j)
            edges[j].add(i)

    output: list[ItemCandidate] = []
    visited: set[int] = set()
    for start, row in enumerate(rows):
        if start in visited:
            continue
        component, pending = set(), [start]
        while pending:
            index = pending.pop()
            if index in component:
                continue
            component.add(index)
            pending.extend(edges[index] - component)
        visited.update(component)
        indices = sorted(component)
        if len(indices) == 1:
            output.append(row)
            continue
        files = [filename for index in indices for filename in _origin_files(rows[index])]
        if (len(files) != len(set(files)) or any(
                component - {index} != edges[index] for index in component)):
            output.extend(rows[index] for index in indices)
            messages.add(f"跨文件候选无法一对一唯一对齐，保留待核验：{row.product_name}")
            continue
        merged = row
        origins: list[dict] = []
        for index in indices:
            candidate = rows[index]
            origins.extend(_origins(candidate))
            changes = {field: getattr(candidate, field) for field in _FIELDS
                       if getattr(merged, field) is None and getattr(candidate, field) is not None}
            if merged.package_code == DEFAULT_PACKAGE_CODE:
                changes["package_code"] = package(candidate)
            merged = merged.model_copy(update=changes)
        if generic_name(row):
            specific = [rows[index] for index in indices
                        if not generic_name(rows[index]) and _specific_name_supported(rows[index])]
            if specific and len({_key(candidate.product_name) for candidate in specific}) == 1:
                merged = merged.model_copy(update={"product_name": specific[0].product_name})
        # Explicit aliases are only applied after an evidenced row correspondence.
        merged = merged.model_copy(update={"package_code": package(merged)})
        provenance = {
            "format": _PROVENANCE,
            "sources": sorted(origins, key=lambda value: json.dumps(value, sort_keys=True)),
            "source_context": {
                filename: {
                    "metadata": source_metadata[filename].model_dump(mode="json")
                    if filename in source_metadata else None,
                    "package_aliases": alias_evidence.get(filename, []),
                } for filename in sorted(set(files))
            },
            "source_links": [sorted(link) for link in sorted(links, key=lambda link: sorted(link))
                             if link <= set(files)],
        }
        for index in indices:
            candidate = rows[index]
            try:
                previous = json.loads(candidate.source_evidence or "")
            except ValueError:
                continue
            if not isinstance(previous, dict) or previous.get("format") != _PROVENANCE:
                continue
            for filename, context in previous.get("source_context", {}).items():
                current = provenance["source_context"].setdefault(filename, context)
                if current["metadata"] is None:
                    current["metadata"] = context.get("metadata")
                for alias in context.get("package_aliases", []):
                    if alias not in current["package_aliases"]:
                        current["package_aliases"].append(alias)
            for link in previous.get("source_links", []):
                if link not in provenance["source_links"]:
                    provenance["source_links"].append(link)
        provenance["source_links"].sort()
        output.append(merged.model_copy(update={
            "source_evidence": json.dumps(provenance, ensure_ascii=False, sort_keys=True),
            "extraction_method": "cross_file_evidence_reconciled",
        }))
        messages.add(f"跨文件证据唯一候选合并 {len(indices)}→1（保留全部原值和来源）：{row.product_name}")
    if warnings is not None:
        warnings.extend(sorted(messages - set(warnings)))
    return sorted(output, key=_sort_key)
