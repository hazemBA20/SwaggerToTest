"""A deliberately small API whose behaviour matches sample_openapi.yaml.

Run with: uvicorn demo_api:app --reload --port 8000
"""
from __future__ import annotations

from enum import Enum

from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr, Field

app = FastAPI(title="Users API", version="1.0.0")


class Role(str, Enum):
    member = "member"
    admin = "admin"


class CreateUser(BaseModel):
    name: str = Field(min_length=1, examples=["Ada Lovelace"])
    email: EmailStr = Field(examples=["ada@example.com"])
    role: Role = Role.member


class User(CreateUser):
    id: int


# Pre-seeding makes GET /users/1 independent from test execution order.
users: dict[int, User] = {1: User(id=1, name="Ada Lovelace", email="ada@example.com")}
next_user_id = 2


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_, exc: RequestValidationError) -> JSONResponse:
    """Use 400 rather than FastAPI's default 422 to match the sample spec."""
    return JSONResponse(status_code=400, content={"detail": exc.errors()})


@app.post("/users", response_model=User, status_code=201, responses={400: {"description": "Invalid request"}})
def create_user(payload: CreateUser) -> User:
    global next_user_id
    user = User(id=next_user_id, **payload.model_dump())
    users[user.id] = user
    next_user_id += 1
    return user


@app.get("/users", response_model=list[User])
def list_users(
    role: Role = Query(..., description="Return users with this role"),
    limit: int = Query(10, ge=1, le=100, description="Maximum records to return"),
) -> list[User]:
    return [user for user in users.values() if user.role == role][:limit]


@app.get("/users/{userId}", response_model=User, responses={404: {"description": "User not found"}})
def get_user(userId: int = Path(ge=1)) -> User:
    user = users.get(userId)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user
