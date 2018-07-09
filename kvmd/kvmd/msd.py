import os
import struct
import asyncio
import types

from typing import Dict
from typing import NamedTuple
from typing import Callable
from typing import Type
from typing import Optional
from typing import Any

import pyudev

import aiofiles
import aiofiles.base

from .logging import get_logger


# =====
class MassStorageError(Exception):
    pass


class IsNotOperationalError(MassStorageError):
    def __init__(self) -> None:
        super().__init__("Missing path for mass-storage device")


class AlreadyConnectedToPcError(MassStorageError):
    def __init__(self) -> None:
        super().__init__("Mass-storage is already connected to Server")


class AlreadyConnectedToKvmError(MassStorageError):
    def __init__(self) -> None:
        super().__init__("Mass-storage is already connected to KVM")


class IsNotConnectedToKvmError(MassStorageError):
    def __init__(self) -> None:
        super().__init__("Mass-storage is not connected to KVM")


class IsBusyError(MassStorageError):
    def __init__(self) -> None:
        super().__init__("Mass-storage is busy (write in progress)")


# =====
class _HardwareInfo(NamedTuple):
    manufacturer: str
    product: str
    serial: str


class _ImageInfo(NamedTuple):
    name: str
    size: int
    complete: bool


class _MassStorageDeviceInfo(NamedTuple):
    path: str
    real: str
    size: int
    hw: Optional[_HardwareInfo]
    image: Optional[_ImageInfo]


_IMAGE_INFO_SIZE = 4096
_IMAGE_INFO_MAGIC_SIZE = 16
_IMAGE_INFO_IMAGE_NAME_SIZE = 256
_IMAGE_INFO_PADS_SIZE = _IMAGE_INFO_SIZE - _IMAGE_INFO_IMAGE_NAME_SIZE - 1 - 8 - _IMAGE_INFO_MAGIC_SIZE * 8
_IMAGE_INFO_FORMAT = ">%dL%dc?Q%dx%dL" % (
    _IMAGE_INFO_MAGIC_SIZE,
    _IMAGE_INFO_IMAGE_NAME_SIZE,
    _IMAGE_INFO_PADS_SIZE,
    _IMAGE_INFO_MAGIC_SIZE,
)
_IMAGE_INFO_MAGIC = [0x1ACE1ACE] * _IMAGE_INFO_MAGIC_SIZE


def _make_image_info_bytes(name: str, size: int, complete: bool) -> bytes:
    return struct.pack(
        _IMAGE_INFO_FORMAT,
        *_IMAGE_INFO_MAGIC,
        *memoryview((  # type: ignore
            name.encode("utf-8")
            + b"\x00" * _IMAGE_INFO_IMAGE_NAME_SIZE
        )[:_IMAGE_INFO_IMAGE_NAME_SIZE]).cast("c"),
        complete,
        size,
        *_IMAGE_INFO_MAGIC,
    )


def _parse_image_info_bytes(data: bytes) -> Optional[_ImageInfo]:
    try:
        parsed = list(struct.unpack(_IMAGE_INFO_FORMAT, data))
    except struct.error:
        pass
    else:
        magic_begin = parsed[:_IMAGE_INFO_MAGIC_SIZE]
        magic_end = parsed[-_IMAGE_INFO_MAGIC_SIZE:]
        if magic_begin == magic_end == _IMAGE_INFO_MAGIC:
            image_name_bytes = b"".join(parsed[_IMAGE_INFO_MAGIC_SIZE:_IMAGE_INFO_MAGIC_SIZE + _IMAGE_INFO_IMAGE_NAME_SIZE])
            return _ImageInfo(
                name=image_name_bytes.decode("utf-8", errors="ignore").strip("\x00").strip(),
                size=parsed[_IMAGE_INFO_MAGIC_SIZE + _IMAGE_INFO_IMAGE_NAME_SIZE + 1],
                complete=parsed[_IMAGE_INFO_MAGIC_SIZE + _IMAGE_INFO_IMAGE_NAME_SIZE],
            )
    return None


def _explore_device(device_path: str) -> Optional[_MassStorageDeviceInfo]:
    # udevadm info -a -p  $(udevadm info -q path -n /dev/sda)
    ctx = pyudev.Context()

    device = pyudev.Devices.from_device_file(ctx, device_path)
    if device.subsystem != "block":
        return None
    try:
        size = device.attributes.asint("size") * 512
    except KeyError:
        return None

    hw_info: Optional[_HardwareInfo] = None
    usb_device = device.find_parent("usb", "usb_device")
    if usb_device:
        hw_info = _HardwareInfo(**{
            attr: usb_device.attributes.asstring(attr).strip()
            for attr in ["manufacturer", "product", "serial"]
        })

    with open(device_path, "rb") as device_file:
        device_file.seek(size - _IMAGE_INFO_SIZE)
        image_info = _parse_image_info_bytes(device_file.read())

    return _MassStorageDeviceInfo(
        path=device_path,
        real=os.path.realpath(device_path),
        size=size,
        image=image_info,
        hw=hw_info,
    )


def _operated_and_locked(method: Callable) -> Callable:
    async def wrap(self: "MassStorageDevice", *args: Any, **kwargs: Any) -> Any:
        if self._device_file:  # pylint: disable=protected-access
            raise IsBusyError()
        if not self._device_path:  # pylint: disable=protected-access
            IsNotOperationalError()
        async with self._lock:  # pylint: disable=protected-access
            return (await method(self, *args, **kwargs))
    return wrap


# =====
class MassStorageDevice:  # pylint: disable=too-many-instance-attributes
    def __init__(
        self,
        device_path: str,
        init_delay: float,
        write_meta: bool,
        loop: asyncio.AbstractEventLoop,
    ) -> None:

        self._device_path = device_path
        self.__init_delay = init_delay
        self.__write_meta = write_meta
        self.__loop = loop

        self.__device_info: Optional[_MassStorageDeviceInfo] = None
        self._lock = asyncio.Lock()
        self._device_file: Optional[aiofiles.base.AiofilesContextManager] = None
        self.__writed = 0

        logger = get_logger(0)
        if self._device_path:
            logger.info("Using %r as mass-storage device", self._device_path)
            try:
                logger.info("Enabled image metadata writing")
                loop.run_until_complete(self.connect_to_kvm(no_delay=True))
            except Exception as err:
                if isinstance(err, MassStorageError):
                    log = logger.error
                else:
                    log = logger.exception
                log("Mass-storage device is not operational: %s", err)
                self._device_path = ""
        else:
            logger.warning("Mass-storage device is not operational")

    @_operated_and_locked
    async def connect_to_kvm(self, no_delay: bool=False) -> None:
        if self.__device_info:
            raise AlreadyConnectedToKvmError()
        # TODO: disable gpio
        if not no_delay:
            await asyncio.sleep(self.__init_delay)
        await self.__load_device_info()
        get_logger().info("Mass-storage device switched to KVM: %s", self.__device_info)

    @_operated_and_locked
    async def connect_to_pc(self) -> None:
        if not self.__device_info:
            raise AlreadyConnectedToPcError()
        # TODO: enable gpio
        self.__device_info = None
        get_logger().info("Mass-storage device switched to Server")

    def get_state(self) -> Dict:
        info = (self.__device_info._asdict() if self.__device_info else None)
        if info:
            info["hw"] = (info["hw"]._asdict() if info["hw"] else None)
            info["image"] = (info["image"]._asdict() if info["image"] else None)
        return {
            "in_operate": bool(self._device_path),
            "connected_to": ("kvm" if self.__device_info else "server"),
            "busy": bool(self._device_file),
            "writed": self.__writed,
            "info": info,
        }

    async def cleanup(self) -> None:
        async with self._lock:
            await self.__close_device_file()
            # TODO: disable gpio

    @_operated_and_locked
    async def __aenter__(self) -> "MassStorageDevice":
        if not self.__device_info:
            raise IsNotConnectedToKvmError()
        self._device_file = await aiofiles.open(self.__device_info.path, mode="w+b", buffering=0)
        self.__writed = 0
        return self

    async def write_image_info(self, name: str, complete: bool) -> None:
        async with self._lock:
            assert self._device_file
            assert self.__device_info
            if self.__write_meta:
                if self.__device_info.size - self.__writed > _IMAGE_INFO_SIZE:
                    await self._device_file.seek(self.__device_info.size - _IMAGE_INFO_SIZE)
                    await self.__write_to_device_file(_make_image_info_bytes(name, self.__writed, complete))
                    await self._device_file.seek(0)
                    await self.__load_device_info()
                else:
                    get_logger().error("Can't write image info because device is full")

    async def write_image_chunk(self, chunk: bytes) -> int:
        async with self._lock:
            await self.__write_to_device_file(chunk)
            self.__writed += len(chunk)
            return self.__writed

    async def __aexit__(
        self,
        _exc_type: Type[BaseException],
        _exc: BaseException,
        _tb: types.TracebackType,
    ) -> None:
        async with self._lock:
            await self.__close_device_file()

    async def __write_to_device_file(self, data: bytes) -> None:
        assert self._device_file
        await self._device_file.write(data)
        await self._device_file.flush()
        await self.__loop.run_in_executor(None, os.fsync, self._device_file.fileno())

    async def __load_device_info(self) -> None:
        device_info = await self.__loop.run_in_executor(None, _explore_device, self._device_path)
        if not device_info:
            raise MassStorageError("Can't explore device %r" % (self._device_path))
        self.__device_info = device_info

    async def __close_device_file(self) -> None:
        try:
            if self._device_file:
                get_logger().info("Closing mass-storage device file ...")
                await self._device_file.close()
        except Exception:
            get_logger().exception("Can't close mass-storage device file")
            # TODO: reset device file
        self._device_file = None
        self.__writed = 0
