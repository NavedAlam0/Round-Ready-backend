from pydantic import BaseModel


class UserResponse(BaseModel):
    id: str
    email: str
    full_name: str
    auth_provider: str

    model_config = {"from_attributes": True}
