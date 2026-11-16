"""Role-based access control (RBAC) and permissions for NEXXA web dashboard.

Defines roles, resource-action mappings, and permission-checking utilities.
Roles:
- admin: Superuser with full control (read, create, update, delete across all resources).
- agent: Operational staff / greenhouse agent. Can read, create, and update stations,
         crops, and batches, and control the robot. Cannot delete records or access
         user management, AI keys, device keys, or system settings.
- viewer: Read-only access to dashboard and logs. Cannot create, update, or delete.
"""
from typing import Any
from fastapi import HTTPException

ROLES = ("admin", "agent", "viewer")

ROLE_LABELS = {
    "admin": "ผู้ดูแลระบบ (Admin)",
    "agent": "เจ้าหน้าที่ (Agent)",
    "viewer": "ผู้เข้าชม (Viewer)",
}

ROLE_BADGE_VARIANTS = {
    "admin": "ok",
    "agent": "info",
    "viewer": "neutral",
}

RESOURCE_LABELS = {
    "overview": "ภาพรวม",
    "robot": "หุ่นยนต์ ESP32",
    "station": "สถานีตรวจ",
    "crop": "ชนิดผัก",
    "batch": "ล็อตผัก",
    "user": "ผู้ดูแลระบบ",
    "key": "คีย์ AI",
    "device": "อุปกรณ์ ESP32",
    "setting": "ตั้งค่าระบบ",
    "audit": "บันทึกการเปลี่ยนแปลง",
}

ACTION_LABELS = {
    "read": "ดูข้อมูล",
    "create": "สร้าง/เพิ่ม",
    "update": "แก้ไข/บันทึก",
    "delete": "ลบ/ยกเลิก",
}

# role -> resource -> set of allowed actions ('read', 'create', 'update', 'delete')
PERMISSIONS: dict[str, dict[str, set[str]]] = {
    "admin": {
        "*": {"read", "create", "update", "delete"},
    },
    "agent": {
        "overview": {"read"},
        "robot": {"read", "update"},
        "station": {"read", "create", "update"},
        "crop": {"read", "create", "update"},
        "batch": {"read", "create", "update"},
        "audit": {"read"},
    },
    "viewer": {
        "overview": {"read"},
        "robot": {"read"},
        "station": {"read"},
        "crop": {"read"},
        "batch": {"read"},
        "audit": {"read"},
    },
}


def get_user_role(user: Any) -> str:
    """Return the normalized role string of an AdminUser. Fails closed if role is absent."""
    if user is None:
        return ""
    role = getattr(user, "role", None)
    return str(role).strip().lower() if role else ""


def get_role_label(role: str) -> str:
    """Return friendly Thai label for a role."""
    return ROLE_LABELS.get(role, role or "ผู้ดูแลระบบ")


def get_role_badge_variant(role: str) -> str:
    """Return CSS badge variant for a role."""
    return ROLE_BADGE_VARIANTS.get(role, "neutral")


def has_permission(user: Any, resource: str, action: str) -> bool:
    """Check if the given user has permission to perform action on resource."""
    if user is None:
        return False
    role = get_user_role(user)
    if role not in PERMISSIONS:
        return False
    role_perms = PERMISSIONS[role]
    # Wildcard grant for superuser
    if "*" in role_perms and action in role_perms["*"]:
        return True
    res_perms = role_perms.get(resource)
    if not res_perms:
        return False
    return action in res_perms


class PermissionDeniedError(HTTPException):
    """Raised when an authenticated user attempts an action outside their permissions."""

    def __init__(self, resource: str, action: str, user: Any):
        self.resource = resource
        self.action = action
        self.user = user
        self.role = get_user_role(user)
        self.role_label = get_role_label(self.role)
        self.resource_label = RESOURCE_LABELS.get(resource, resource)
        self.action_label = ACTION_LABELS.get(action, action)
        detail = (
            f"ไม่มีสิทธิ์ดำเนินการ '{self.action_label}' ในส่วน '{self.resource_label}' "
            f"(บทบาทของคุณคือ {self.role_label})"
        )
        super().__init__(status_code=403, detail=detail)
