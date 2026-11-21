from .admin import router as admin_router
from .edit import router as edit_router
from .staff import router as staff_router
from .user import router as user_router

__all__ = ["admin_router", "edit_router", "staff_router", "user_router"]
