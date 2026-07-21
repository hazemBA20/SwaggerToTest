"""A self-contained CRUD API for exercising OpenAPI-to-test generation.

Run: uvicorn demo_api:app --reload --port 8000
Docs: http://localhost:8000/docs
"""
from __future__ import annotations

from enum import Enum
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, Path, Query, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr, Field, model_validator

app = FastAPI(title="Team Directory API", version="1.0.0", description="Demo CRUD API for contract-test generation.")


class Role(str, Enum):
    member = "member"
    manager = "manager"
    admin = "admin"


class UserCreate(BaseModel):
    name: str = Field(min_length=2, max_length=80, examples=["Katherine Johnson"])
    email: EmailStr = Field(examples=["katherine@example.com"])
    role: Role = Role.member
    active: bool = True


class UserUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=80)
    email: EmailStr | None = None
    role: Role | None = None
    active: bool | None = None

    @model_validator(mode="after")
    def at_least_one_value(self) -> "UserUpdate":
        if not self.model_fields_set:
            raise ValueError("At least one field must be supplied")
        return self


class User(UserCreate):
    id: int = Field(ge=1)


class UserPage(BaseModel):
    items: list[User]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=100)


users: dict[int, User] = {}
next_user_id = 1


def reset_data() -> None:
    """Restore deterministic demo data; only used by the hidden test endpoint."""
    global users, next_user_id
    users = {
        1: User(id=1, name="Ada Lovelace", email="ada@example.com", role=Role.admin),
        2: User(id=2, name="Grace Hopper", email="grace@example.com", role=Role.manager),
        123: User(id=123, name="Linus Torvalds", email="linus@example.com", role=Role.member),
    }
    next_user_id = 4


reset_data()


@app.post("/__test/reset", status_code=status.HTTP_204_NO_CONTENT, include_in_schema=False)
def reset_demo_data() -> Response:
    """Demo-only isolation hook. Never expose an equivalent endpoint in production."""
    reset_data()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_, exc: RequestValidationError) -> JSONResponse:
    """Expose all invalid request inputs as HTTP 400 for this demo contract."""
    return JSONResponse(status_code=400, content={"detail": jsonable_encoder(exc.errors())})


RequestId = Annotated[str | None, Header(alias="X-Request-ID", min_length=8, max_length=64, description="Optional request correlation ID")]


@app.get("/users", response_model=UserPage, tags=["Users"])
def list_users(
    request: Request,
    request_id: RequestId = None,
    role: Role | None = Query(default=None),
    active: bool | None = Query(default=None),
    search: str | None = Query(default=None, min_length=2, max_length=50),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=100),
) -> UserPage:
    # FastAPI/Pydantic normally treats strings such as "yes" and "1" as true.
    # This demo follows the stricter OpenAPI boolean contract: only true/false.
    raw_active = request.query_params.get("active")
    if raw_active is not None and raw_active not in {"true", "false"}:
        raise HTTPException(status_code=400, detail="active must be 'true' or 'false'")
    results = list(users.values())
    if role is not None:
        results = [user for user in results if user.role == role]
    if active is not None:
        results = [user for user in results if user.active == active]
    if search:
        term = search.lower()
        results = [user for user in results if term in user.name.lower() or term in user.email.lower()]
    total = len(results)
    start = (page - 1) * page_size
    return UserPage(items=results[start:start + page_size], total=total, page=page, page_size=page_size)


@app.post("/users", response_model=User, status_code=status.HTTP_201_CREATED, tags=["Users"], responses={400: {"description": "Invalid request"}})
def create_user(payload: UserCreate, request_id: RequestId = None) -> User:
    global next_user_id
    if any(user.email == payload.email for user in users.values()):
        raise HTTPException(status_code=409, detail="A user with this email already exists")
    user = User(id=next_user_id, **payload.model_dump())
    users[user.id] = user
    next_user_id += 1
    return user


@app.get("/users/{user_id}", response_model=User, tags=["Users"], responses={404: {"description": "User not found"}})
def get_user(user_id: int = Path(ge=1), request_id: RequestId = None) -> User:
    user = users.get(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@app.patch("/users/{user_id}", response_model=User, tags=["Users"], responses={400: {"description": "Invalid request"}, 404: {"description": "User not found"}})
def update_user(payload: UserUpdate, user_id: int = Path(ge=1), request_id: RequestId = None) -> User:
    existing = users.get(user_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="User not found")
    updates = payload.model_dump(exclude_unset=True)
    if "email" in updates and any(user.id != user_id and user.email == updates["email"] for user in users.values()):
        raise HTTPException(status_code=409, detail="A user with this email already exists")
    users[user_id] = existing.model_copy(update=updates)
    return users[user_id]


@app.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Users"], responses={404: {"description": "User not found"}})
def delete_user(user_id: int = Path(ge=1), request_id: RequestId = None) -> Response:
    if user_id not in users:
        raise HTTPException(status_code=404, detail="User not found")
    del users[user_id]
    return Response(status_code=status.HTTP_204_NO_CONTENT)
