from pydantic import BaseModel, ConfigDict, Field


class ModuleSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ClassroomSettings(ModuleSettings):
    auto_sign: bool = True
    signin_offset_minutes: int = Field(default=-5, ge=-5, le=29)


class BoyaSettings(ModuleSettings):
    auto_sign: bool = True
    filter_by_campus: bool = True


class SunshineSettings(ModuleSettings):
    reminder_time: str = Field(default="18:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class TdSettings(ModuleSettings):
    term_target: int = Field(default=32, ge=1, le=10000)


MODULE_SETTINGS = {
    "classroom": ClassroomSettings,
    "boya": BoyaSettings,
    "ygdk": SunshineSettings,
    "td": TdSettings,
}
