"""Extract SRS OptiMelt .opm measurement data without MeltView.

The format is a little-endian binary container.  This parser targets the
SRS_OPTIMELT_DATA_FILE variant validated against an MPA100/MeltView file.
It extracts metadata, detected transition points, the measurement time series,
and (optionally) the per-frame 336 x 84 grayscale camera images.
"""
from __future__ import annotations

import argparse
import csv
import json
import struct
from dataclasses import dataclass, asdict
from pathlib import Path


MAGIC = b"SRS_OPTIMELT_DATA_FILE\r\n"
FRAME_MARKER = b"\x78\x56\x34\x12"
SUPPORTED_VERSIONS = {4}


class OPMFormatError(ValueError):
    pass


@dataclass
class Frame:
    time_s: float
    temp_c: float
    left: float
    center: float
    right: float
    image: bytes


def _u16(data: bytes, pos: int) -> tuple[int, int]:
    return struct.unpack_from("<H", data, pos)[0], pos + 2


def _u32(data: bytes, pos: int) -> tuple[int, int]:
    return struct.unpack_from("<I", data, pos)[0], pos + 4


def _f32(data: bytes, pos: int) -> tuple[float, int]:
    return struct.unpack_from("<f", data, pos)[0], pos + 4


def _lp_ascii(data: bytes, pos: int) -> tuple[str, int]:
    length, pos = _u32(data, pos)
    if length > 4096 or pos + length > len(data):
        raise OPMFormatError(f"Invalid length-prefixed string at 0x{pos-4:x}")
    return data[pos:pos + length].decode("ascii", errors="replace"), pos + length


def parse_opm(path: str | Path, include_images: bool = False) -> dict:
    path = Path(path)
    data = path.read_bytes()
    if not data.startswith(MAGIC):
        raise OPMFormatError("Not an SRS OptiMelt data file")

    # The validated variant has a 17-byte reserved block after the magic,
    # followed by fixed header integers and then length-prefixed ASCII fields.
    pos = len(MAGIC) + 17
    version, pos = _u32(data, pos)
    if version not in SUPPORTED_VERSIONS:
        raise OPMFormatError(
            f"Unsupported OPM format version {version}; supported: {sorted(SUPPORTED_VERSIONS)}"
        )
    image_width, pos = _u32(data, pos)
    image_record_bytes, pos = _u32(data, pos)
    frame_count, pos = _u32(data, pos)
    _unknown_b, pos = _u32(data, pos)
    _unknown_c, pos = _u32(data, pos)
    serial, pos = _u32(data, pos)

    instrument_name, pos = _lp_ascii(data, pos)
    acquired_date, pos = _lp_ascii(data, pos)
    acquired_time, pos = _lp_ascii(data, pos)
    chemical_name, pos = _lp_ascii(data, pos)
    batch_number, pos = _lp_ascii(data, pos)

    start_c, pos = _f32(data, pos)
    stop_c, pos = _f32(data, pos)
    heating_rate, pos = _f32(data, pos)
    onset_threshold, pos = _u16(data, pos)
    single_threshold, pos = _u16(data, pos)
    clear_threshold, pos = _u16(data, pos)

    marker = data.find(FRAME_MARKER, pos)
    if marker < 0:
        raise OPMFormatError("Frame marker 0x12345678 not found")
    if marker < 60:
        raise OPMFormatError("Header before frame marker is too short")

    # Nine transition-point floats sit 56 bytes before the marker.
    vals = struct.unpack_from("<9f", data, marker - 56)
    points = {
        "onset": dict(zip(("left", "center", "right"), vals[0:3])),
        "clear": dict(zip(("left", "center", "right"), vals[3:6])),
        "single": dict(zip(("left", "center", "right"), vals[6:9])),
    }

    pos = marker + 4
    raw_frames: list[tuple[float, float, float, float, float, bytes]] = []
    for index in range(frame_count):
        if pos + 24 > len(data):
            raise OPMFormatError(f"Truncated frame header at frame {index}")
        absolute_t, temp, left, center, right = struct.unpack_from("<5f", data, pos)
        pos += 20
        image_size, pos = _u32(data, pos)
        if image_size > len(data) - pos:
            raise OPMFormatError(f"Invalid image size at frame {index}")
        image = data[pos:pos + image_size] if include_images else b""
        pos += image_size
        raw_frames.append((absolute_t, temp, left, center, right, image))

    t0 = raw_frames[0][0] if raw_frames else 0.0
    frames = [
        Frame(
            time_s=round(float(t - t0), 6),
            temp_c=float(temp),
            left=float(left),
            center=float(center),
            right=float(right),
            image=image,
        )
        for t, temp, left, center, right, image in raw_frames
    ]

    inferred_height = (image_record_bytes - 24) // image_width
    image_size = image_width * inferred_height
    return {
        "metadata": {
            "format": "SRS_OPTIMELT_DATA_FILE",
            "format_version": version,
            "instrument_name": instrument_name,
            "instrument_serial_number": serial,
            "acquired_date": acquired_date,
            "acquired_time": acquired_time,
            "chemical_name": chemical_name,
            "batch_number": batch_number,
            "start_temperature_c": start_c,
            "stop_temperature_c": stop_c,
            "heating_rate_c_min": heating_rate,
            "onset_threshold_percent": onset_threshold,
            "single_threshold_percent": single_threshold,
            "clear_threshold_percent": clear_threshold,
            "frame_count": frame_count,
            "camera_width": image_width,
            "camera_height_inferred": inferred_height if image_record_bytes >= image_size else None,
        },
        "points": points,
        "frames": frames,
        "bytes_consumed": pos,
        "file_size": len(data),
    }


def write_outputs(result: dict, output_dir: str | Path, stem: str, images: bool = False) -> None:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    summary = {
        "metadata": result["metadata"],
        "points": result["points"],
        "bytes_consumed": result["bytes_consumed"],
        "file_size": result["file_size"],
    }
    (output_dir / f"{stem}_metadata.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    with (output_dir / f"{stem}_timeseries.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["Time(s)", "Temp(C)", "Left", "Center", "Right"])
        for f in result["frames"]:
            writer.writerow([f.time_s, f.temp_c, f.left, f.center, f.right])

    if images:
        try:
            from PIL import Image
        except ImportError as exc:
            raise RuntimeError("Pillow is required for --images") from exc
        width = result["metadata"]["camera_width"]
        height = result["metadata"]["camera_height_inferred"]
        if not height:
            raise OPMFormatError("Could not infer camera image height")
        image_dir = output_dir / f"{stem}_frames"
        image_dir.mkdir(exist_ok=True)
        for index, frame in enumerate(result["frames"]):
            if len(frame.image) == width * height:
                Image.frombytes("L", (width, height), frame.image).save(
                    image_dir / f"frame_{index:04d}.png"
                )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("opm", type=Path)
    parser.add_argument("-o", "--output-dir", type=Path, default=Path("opm_output"))
    parser.add_argument("--images", action="store_true", help="also extract grayscale camera frames")
    args = parser.parse_args()

    result = parse_opm(args.opm, include_images=args.images)
    write_outputs(result, args.output_dir, args.opm.stem, images=args.images)
    print(json.dumps({"metadata": result["metadata"], "points": result["points"]}, indent=2))


if __name__ == "__main__":
    main()
