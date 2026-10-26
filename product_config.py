#!/usr/bin/env python3
"""Server-side product configuration store.

Plan refs: F11, PLAN_FINAL amendment 5.
- JSON store with atomic replace and a revision token for multi-client detection
- one-time migration from the legacy product_models.json (program -> program1,
  program2 is NOT guessed)
- switchChamber is stored but has no actuator; saved true never means executed
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import uuid
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent
_LOCK = threading.RLock()

PRODUCT_KEYS = ("id", "model", "program1", "program2", "labelTemplate",
                "supplierCode", "switchChamber")


def _store_path() -> Path:
    return Path(os.environ.get("ATEQ_PRODUCT_CONFIG", str(ROOT / "products.json")))


def _legacy_path() -> Path:
    return Path(os.environ.get("ATEQ_LEGACY_PRODUCT_MODELS",
                               str(ROOT / "product_models.json")))


def _empty_store() -> dict:
    return {"revision": 0, "products": []}


class StoreUnavailable(Exception):
    """Raised when the store exists but cannot be read/parsed."""


def _read_store() -> dict:
    path = _store_path()
    if not path.exists():
        return _empty_store()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise StoreUnavailable(f"product config unreadable: {exc}")
    if not isinstance(data, dict) or not isinstance(data.get("products"), list):
        raise StoreUnavailable("product config structure invalid")
    data.setdefault("revision", 0)
    return data


def store_health() -> dict:
    try:
        _read_store()
        return {"ok": True, "error": None, "path": str(_store_path())}
    except StoreUnavailable as exc:
        return {"ok": False, "error": str(exc), "path": str(_store_path())}


def _write_store(store: dict) -> None:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    temp.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def _coerce_program(value, minimum, maximum):
    if isinstance(value, bool):
        return None
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    if minimum <= number <= maximum:
        return number
    return None


def _normalize_product(raw: dict, index: int, warnings: list) -> Optional[dict]:
    if not isinstance(raw, dict):
        warnings.append(f"第{index + 1}行不是对象，已跳过")
        return None
    model = str(raw.get("model") or "").strip()
    if not model:
        warnings.append(f"第{index + 1}行缺少型号，已跳过")
        return None
    if len(model) > 100:
        warnings.append(f"型号过长已截断: {model[:20]}...")
        model = model[:100]
    program1 = _coerce_program(raw.get("program1"), 1, 255)
    if program1 is None:
        warnings.append(f"{model}: program1 无效，已跳过")
        return None
    program2 = _coerce_program(raw.get("program2"), 0, 255)
    if program2 is None:
        program2 = 0
    label_template = str(raw.get("labelTemplate") or "")[:200]
    supplier_code = str(raw.get("supplierCode") or "")[:200]
    switch_chamber = bool(raw.get("switchChamber"))
    if switch_chamber:
        warnings.append(f"{model}: switchChamber=true 已保存，但当前无执行器，不会执行动作")
    product = {
        "id": raw.get("id"),
        "model": model,
        "program1": program1,
        "program2": program2,
        "labelTemplate": label_template,
        "supplierCode": supplier_code,
        "switchChamber": switch_chamber,
    }
    return product


def _validate_list(products: list) -> tuple:
    """Lenient validation used by migration: warns, skips unusable rows."""
    warnings: list = []
    normalized: list = []
    seen_models = set()
    used_ids = set()
    for index, raw in enumerate(products):
        product = _normalize_product(raw, index, warnings)
        if product is None:
            continue
        if product["model"] in seen_models:
            warnings.append(f"重复型号已跳过: {product['model']}")
            continue
        seen_models.add(product["model"])
        product_id = product.get("id")
        if product_id is None or product_id in used_ids:
            product_id = index + 1
            while product_id in used_ids:
                product_id += 1
        used_ids.add(product_id)
        product["id"] = product_id
        normalized.append(product)
    return normalized, warnings


class ValidationError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _validate_list_strict(products: list) -> tuple:
    """Strict validation for explicit PUT: rejects instead of guessing."""
    if not isinstance(products, list):
        raise ValidationError("products 必须为数组")
    warnings: list = []
    normalized: list = []
    seen_models = set()
    used_ids = set()
    for index, raw in enumerate(products):
        if not isinstance(raw, dict):
            raise ValidationError(f"第{index + 1}行不是对象")
        model = str(raw.get("model") or "").strip()
        if not model:
            raise ValidationError(f"第{index + 1}行缺少型号")
        if len(model) > 100:
            raise ValidationError(f"{model[:20]}... 型号过长(>100)")
        if model in seen_models:
            raise ValidationError(f"重复型号: {model}")
        seen_models.add(model)
        program1 = _coerce_program(raw.get("program1"), 1, 255)
        if program1 is None:
            raise ValidationError(f"{model}: program1 必须为 1..255 的整数")
        raw_p2 = raw.get("program2")
        if raw_p2 in (None, ""):
            program2 = 0
        else:
            program2 = _coerce_program(raw_p2, 0, 255)
        if program2 is None:
            raise ValidationError(f"{model}: program2 必须为 0..255 的整数")
        switch_chamber = bool(raw.get("switchChamber"))
        if switch_chamber:
            warnings.append(f"{model}: switchChamber=true 已保存，但当前无执行器，不会执行动作")
        product_id = raw.get("id")
        if product_id is None or product_id in used_ids:
            product_id = index + 1
            while product_id in used_ids:
                product_id += 1
        used_ids.add(product_id)
        normalized.append({
            "id": product_id,
            "model": model,
            "program1": program1,
            "program2": program2,
            "labelTemplate": str(raw.get("labelTemplate") or "")[:200],
            "supplierCode": str(raw.get("supplierCode") or "")[:200],
            "switchChamber": switch_chamber,
        })
    return normalized, warnings


def _migrate_legacy_if_needed() -> None:
    with _LOCK:
        if _store_path().exists():
            return
        legacy = _legacy_path()
        if not legacy.exists():
            return
        try:
            raw = json.loads(legacy.read_text(encoding="utf-8-sig"))
        except Exception:
            return
        if not isinstance(raw, list):
            return
        products = []
        migration_warnings = []
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                migration_warnings.append(f"第{index + 1}条不是对象，已跳过")
                continue
            model = item.get("model")
            # An EXPLICIT invalid program2 must not silently become single-stage:
            # skip the row with a warning instead of guessing program2=0.
            explicit_p2 = "program2" in item and item.get("program2") not in (None, "")
            if explicit_p2 and _coerce_program(item.get("program2"), 0, 255) is None:
                migration_warnings.append(
                    f"{model}: 旧配置 program2={item.get('program2')!r} 无效，已跳过该行")
                continue
            products.append({
                "id": item.get("id", index + 1),
                "model": model,
                "program1": item.get("program", item.get("program1")),
                "program2": item.get("program2", 0) if explicit_p2 else 0,
                "labelTemplate": item.get("labelTemplate", ""),
                "supplierCode": item.get("supplierCode", ""),
                "switchChamber": bool(item.get("switchChamber", False)),
            })
        normalized, _warnings = _validate_list(products)
        store = {"revision": 1, "products": normalized}
        try:
            _write_store(store)
        except Exception:
            pass


def get_revision() -> int:
    _migrate_legacy_if_needed()
    with _LOCK:
        return int(_read_store().get("revision", 0))


def list_products() -> list:
    _migrate_legacy_if_needed()
    with _LOCK:
        return list(_read_store().get("products", []))


def find_product(model: str) -> Optional[dict]:
    name = str(model or "").strip()
    if not name:
        return None
    for product in list_products():
        if str(product.get("model") or "").strip() == name:
            return dict(product)
    return None


def replace_products(products: list, revision=None) -> dict:
    _migrate_legacy_if_needed()
    with _LOCK:
        current = _read_store()
        if revision is not None:
            try:
                if int(revision) != int(current.get("revision", 0)):
                    return {"success": False, "error_code": "REVISION_CONFLICT",
                            "message": "配置已被其他客户端修改，请刷新后重试",
                            "revision": current.get("revision", 0)}
            except (TypeError, ValueError):
                return {"success": False, "error_code": "VALIDATION",
                        "message": "revision 必须为整数"}
        try:
            normalized, warnings = _validate_list_strict(products)
        except ValidationError as exc:
            return {"success": False, "error_code": "VALIDATION",
                    "message": exc.message,
                    "revision": current.get("revision", 0)}
        new_revision = int(current.get("revision", 0)) + 1
        store = {"revision": new_revision, "products": normalized}
        try:
            _write_store(store)
        except Exception as exc:
            return {"success": False, "error_code": "CONFIG_WRITE_FAILED",
                    "message": str(exc)}
        return {"success": True, "revision": new_revision,
                "products": normalized, "warnings": warnings}


# ------------------------------------------------------------ active product
# The product currently selected for production is server state: it survives a
# service restart and is never cleared by a browser that has no selection.

def _active_path() -> Path:
    default = _store_path().with_name("active_product.json")
    return Path(os.environ.get("ATEQ_ACTIVE_PRODUCT", str(default)))


def get_active_model() -> Optional[str]:
    """Persisted active product model name, or None when never selected/unreadable."""
    path = _active_path()
    with _LOCK:
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
    model = str((data or {}).get("model") or "").strip() if isinstance(data, dict) else ""
    return model or None


def set_active_model(model: str) -> None:
    """Persist the active product; only an existing configured model is accepted."""
    name = str(model or "").strip()
    if not name or find_product(name) is None:
        raise ValueError(f"产品型号不在服务器配置中: {name!r}")
    path = _active_path()
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        temp.write_text(json.dumps({"model": name}, ensure_ascii=False), encoding="utf-8")
        os.replace(temp, path)


def get_active_product() -> Optional[dict]:
    """Active product resolved against the current store (None if removed)."""
    return find_product(get_active_model() or "")
