from datetime import datetime

from pydantic import BaseModel


class ComponentStatus(BaseModel):
    configured: bool
    connected: bool
    detail: str
    checked_at: datetime


class MotorFeedback(BaseModel):
    motor_id: int
    rpm: int | None
    current_raw: int | None


class Battery(BaseModel):
    """All optional: the flight controller reports "unknown" for any of these,
    and so does a vehicle whose FC is not talking."""

    voltage_v: float | None
    current_a: float | None
    remaining_percent: int | None


class Attitude(BaseModel):
    roll_deg: float | None
    pitch_deg: float | None
    yaw_deg: float | None


class PiHealth(BaseModel):
    """The Raspberry Pi's own health. Every field is optional: off a Pi, or on
    a kernel without the reading, it is simply not known.

    `*_now` is the firmware's state at the moment of reading; `*_since_boot`
    is sticky, so a brownout that has already passed still shows.
    """

    cpu_temp_c: float | None
    load_1m: float | None
    memory_available_mb: int | None
    memory_available_percent: int | None
    disk_free_mb: int | None
    disk_free_percent: int | None
    throttled_raw: str | None
    undervoltage_now: bool | None
    freq_capped_now: bool | None
    throttled_now: bool | None
    soft_temp_limit_now: bool | None
    undervoltage_since_boot: bool | None
    freq_capped_since_boot: bool | None
    throttled_since_boot: bool | None
    soft_temp_limit_since_boot: bool | None
    warnings: list[str]


class VehicleHealthResponse(BaseModel):
    status: str
    service: str
    timestamp: datetime
    components: dict[str, ComponentStatus]


class VehicleStatusResponse(BaseModel):
    service: str
    timestamp: datetime
    motor_device: str | None
    fc_device: str | None
    motor_ids: dict[str, list[int]]
    components: dict[str, ComponentStatus]
    battery: Battery
    attitude: Attitude
    pi: PiHealth
    motor_feedback: list[MotorFeedback]


class CameraStatusResponse(BaseModel):
    service: str
    timestamp: datetime
    running: bool
    clients: int
    width: int
    height: int
    framerate: int
    jpeg_quality: int
    encoder: str | None
    frames_captured: int
    last_frame_at: datetime | None
    component: ComponentStatus


class CameraCommandResponse(BaseModel):
    service: str
    action: str
    running: bool
    detail: str
    timestamp: datetime


class StartMotorsRequest(BaseModel):
    rpm: int


class MotorCommandResponse(BaseModel):
    service: str
    action: str
    target_rpm: int
    current_rpm: int
    detail: str
    timestamp: datetime
