"""
PixelMed-backed JPEG Baseline selective block redaction helpers.

The Java side uses PixelMed's com.pixelmed.codec.jpeg.Parse.parse() API. This
module keeps the bridge optional: if Java or the bundled jars are missing,
callers can catch PixelMedUnavailableError and use another implementation. A
developer-only runtime compile fallback can be enabled with
DEIDECHO_PIXELMED_ALLOW_RUNTIME_COMPILE=1.
"""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

try:
    from pydicom.encaps import encapsulate, generate_frames
except Exception:  # pragma: no cover
    encapsulate = None
    generate_frames = None


Rectangle = Tuple[int, int, int, int]

_MAGIC = b"PMJR1"
_DEFAULT_TIMEOUT_SECONDS = 300
_BRIDGE_SOURCE = Path(__file__).resolve().parent / "java" / "PixelMedRedactionBridge.java"
_PATCHED_ENTROPY_SOURCE = (
    Path(__file__).resolve().parent
    / "java"
    / "com"
    / "pixelmed"
    / "codec"
    / "jpeg"
    / "EntropyCodedSegment.java"
)
_BUNDLED_CODEC_JAR = Path(__file__).resolve().parent / "vendor" / "pixelmed_codec.jar"
_BUNDLED_BRIDGE_JAR = (
    Path(__file__).resolve().parent / "vendor" / "deidecho_pixelmed_bridge.jar"
)
_ALLOW_RUNTIME_COMPILE_ENV = "DEIDECHO_PIXELMED_ALLOW_RUNTIME_COMPILE"


class PixelMedUnavailableError(RuntimeError):
    """Raised when Java, required jars, or optional bridge compilation are unavailable."""


class PixelMedRedactionError(RuntimeError):
    """Raised when the PixelMed bridge runs but fails to redact a JPEG stream."""


def _resolve_executable(env_name: str, executable: str) -> str:
    configured = os.getenv(env_name)
    if configured:
        if Path(configured).is_file() and os.access(configured, os.X_OK):
            return configured
        raise PixelMedUnavailableError(
            f"{env_name} is set but is not executable: {configured}"
        )

    found = shutil.which(executable)
    if found:
        return found
    raise PixelMedUnavailableError(f"{executable} not found on PATH")


def resolve_pixelmed_codec_jar() -> Path:
    configured = os.getenv("DEIDECHO_PIXELMED_CODEC_JAR")
    jar_path = Path(configured).expanduser() if configured else _BUNDLED_CODEC_JAR
    if jar_path.is_file():
        return jar_path
    raise PixelMedUnavailableError(
        "PixelMed codec jar not found. Set DEIDECHO_PIXELMED_CODEC_JAR or "
        f"place it at {str(_BUNDLED_CODEC_JAR)}"
    )


def resolve_pixelmed_bridge_jar() -> Path:
    configured = os.getenv("DEIDECHO_PIXELMED_BRIDGE_JAR")
    jar_path = Path(configured).expanduser() if configured else _BUNDLED_BRIDGE_JAR
    if jar_path.is_file():
        return jar_path
    raise PixelMedUnavailableError(
        "PixelMed bridge jar not found. Set DEIDECHO_PIXELMED_BRIDGE_JAR or "
        f"place it at {str(_BUNDLED_BRIDGE_JAR)}"
    )


def pixelmed_runtime_compile_allowed() -> bool:
    value = os.getenv(_ALLOW_RUNTIME_COMPILE_ENV, "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _bridge_cache_dir(sources: Sequence[Path], jar_path: Path) -> Path:
    cache_root = Path(
        os.getenv(
            "DEIDECHO_PIXELMED_BRIDGE_CACHE",
            str(Path(tempfile.gettempdir()) / "deidecho_pixelmed_bridge"),
        )
    )
    digest = hashlib.sha1()
    for source in sources:
        digest.update(str(source).encode("utf-8"))
        digest.update(str(source.stat().st_mtime_ns).encode("ascii"))
    digest.update(str(jar_path).encode("utf-8"))
    digest.update(str(jar_path.stat().st_mtime_ns).encode("ascii"))
    return cache_root / digest.hexdigest()[:16]


def compile_pixelmed_bridge(
    *,
    javac_path: Optional[str] = None,
    jar_path: Optional[Path] = None,
) -> Path:
    if not _BRIDGE_SOURCE.is_file():
        raise PixelMedUnavailableError(f"Bridge source not found: {_BRIDGE_SOURCE}")
    sources = [_BRIDGE_SOURCE]
    if _PATCHED_ENTROPY_SOURCE.is_file():
        sources.append(_PATCHED_ENTROPY_SOURCE)

    jar = jar_path or resolve_pixelmed_codec_jar()
    javac = javac_path or _resolve_executable("DEIDECHO_JAVAC", "javac")
    class_dir = _bridge_cache_dir(sources, jar)
    class_file = class_dir / "PixelMedRedactionBridge.class"
    newest_source_mtime = max(source.stat().st_mtime for source in sources)
    if class_file.is_file() and class_file.stat().st_mtime >= newest_source_mtime:
        return class_dir

    class_dir.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            javac,
            "-cp",
            str(jar),
            "-d",
            str(class_dir),
        ]
        + [str(source) for source in sources],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        detail = stderr or stdout or f"javac exited {result.returncode}"
        raise PixelMedUnavailableError(f"Could not compile PixelMed bridge: {detail}")
    return class_dir


def resolve_pixelmed_bridge_runtime(
    *,
    javac_path: Optional[str] = None,
    jar_path: Optional[Path] = None,
) -> Dict[str, str]:
    """
    Resolve the Java classpath for PixelMed redaction.

    Normal releases use the precompiled bridge jar and do not require javac.
    Runtime compilation is retained only for development and must be explicitly
    enabled with DEIDECHO_PIXELMED_ALLOW_RUNTIME_COMPILE=1.
    """
    codec_jar = jar_path or resolve_pixelmed_codec_jar()
    try:
        bridge_jar = resolve_pixelmed_bridge_jar()
        return {
            "classpath": os.pathsep.join([str(bridge_jar), str(codec_jar)]),
            "bridge_jar_path": str(bridge_jar),
            "class_dir": "",
            "javac_path": "",
        }
    except PixelMedUnavailableError:
        if not pixelmed_runtime_compile_allowed():
            raise

    javac = javac_path or _resolve_executable("DEIDECHO_JAVAC", "javac")
    class_dir = compile_pixelmed_bridge(javac_path=javac, jar_path=codec_jar)
    return {
        "classpath": os.pathsep.join([str(class_dir), str(codec_jar)]),
        "bridge_jar_path": "",
        "class_dir": str(class_dir),
        "javac_path": javac,
    }


def _java_version(java_path: str) -> str:
    result = subprocess.run(
        [java_path, "-version"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    detail = ((result.stderr or "") + "\n" + (result.stdout or "")).strip()
    first_line = detail.splitlines()[0] if detail else ""
    if result.returncode != 0:
        raise PixelMedUnavailableError(
            f"java -version failed rc={result.returncode}: {first_line}"
        )
    return first_line


def _probe_pixelmed_bridge(java_path: str, classpath: str) -> None:
    payload = _write_payload([], [])
    result = subprocess.run(
        [
            java_path,
            "-XX:ActiveProcessorCount=1",
            "-Xss512k",
            "-cp",
            classpath,
            "PixelMedRedactionBridge",
        ],
        input=payload,
        capture_output=True,
        timeout=30,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        stdout = result.stdout.decode("utf-8", errors="replace").strip()
        detail = stderr or stdout or f"java exited {result.returncode}"
        raise PixelMedUnavailableError(
            f"PixelMed bridge smoke test failed: {detail[:500]}"
        )
    frames = _read_redacted_frames(result.stdout)
    if frames:
        raise PixelMedUnavailableError("PixelMed bridge smoke test returned frames")


def inspect_pixelmed_runtime() -> Dict[str, Any]:
    """
    Return structured diagnostics for the PixelMed Java bridge runtime.
    """
    runtime_compile_allowed = pixelmed_runtime_compile_allowed()
    diagnostics: Dict[str, Any] = {
        "available": False,
        "java_path": "",
        "java_version": "",
        "javac_path": "",
        "jar_path": "",
        "bridge_jar_path": "",
        "class_dir": "",
        "runtime_compile_allowed": runtime_compile_allowed,
        "bridge_probe": "",
        "conda_default_env": os.getenv("CONDA_DEFAULT_ENV", ""),
        "conda_prefix": os.getenv("CONDA_PREFIX", ""),
        "error": "",
    }

    try:
        jar = resolve_pixelmed_codec_jar()
        diagnostics["jar_path"] = str(jar)

        java = _resolve_executable("DEIDECHO_JAVA", "java")
        diagnostics["java_path"] = java
        diagnostics["java_version"] = _java_version(java)

        runtime = resolve_pixelmed_bridge_runtime(jar_path=jar)
        diagnostics["bridge_jar_path"] = runtime["bridge_jar_path"]
        diagnostics["class_dir"] = runtime["class_dir"]
        diagnostics["javac_path"] = runtime["javac_path"]

        _probe_pixelmed_bridge(java, runtime["classpath"])
        diagnostics["bridge_probe"] = "ok"
        diagnostics["available"] = True
    except PixelMedUnavailableError as exc:
        diagnostics["error"] = str(exc)

    return diagnostics


def pixelmed_bridge_available() -> bool:
    return bool(inspect_pixelmed_runtime()["available"])


def mask_to_redaction_rectangles(redact_mask: np.ndarray) -> List[Rectangle]:
    """
    Convert a boolean redaction mask to exact non-overlapping rectangles.

    Rectangles are (x, y, width, height). The conversion is exact: it never
    includes keep pixels inside a rectangle, which avoids asking PixelMed to
    redact JPEG blocks that are only adjacent to the target mask.
    """
    arr = np.asarray(redact_mask).astype(bool)
    if arr.ndim != 2:
        raise ValueError(f"Need a 2D mask, got shape {arr.shape}")
    rows, cols = arr.shape
    rectangles: List[Rectangle] = []
    active = {}

    for y in range(rows + 1):
        current_runs = []
        if y < rows:
            x = 0
            while x < cols:
                if not arr[y, x]:
                    x += 1
                    continue
                x0 = x
                while x < cols and arr[y, x]:
                    x += 1
                current_runs.append((x0, x))

        current = set(current_runs)
        for run in list(active):
            if run not in current:
                y0 = active.pop(run)
                x0, x1 = run
                rectangles.append((x0, y0, x1 - x0, y - y0))

        for run in current_runs:
            if run not in active:
                active[run] = y

    return rectangles


def _write_payload(
    frames: Sequence[bytes], frame_rectangles: Sequence[Sequence[Rectangle]]
) -> bytes:
    if len(frames) != len(frame_rectangles):
        raise ValueError(
            f"Need one rectangle list per frame: {len(frames)} frames, "
            f"{len(frame_rectangles)} rectangle lists"
        )

    payload = io.BytesIO()
    payload.write(_MAGIC)
    payload.write(struct.pack(">i", len(frames)))
    for frame, rectangles in zip(frames, frame_rectangles):
        payload.write(struct.pack(">i", len(rectangles)))
        for x, y, width, height in rectangles:
            payload.write(struct.pack(">iiii", int(x), int(y), int(width), int(height)))
        payload.write(struct.pack(">i", len(frame)))
        payload.write(frame)
    return payload.getvalue()


def _read_redacted_frames(payload: bytes) -> List[bytes]:
    stream = io.BytesIO(payload)
    magic = stream.read(len(_MAGIC))
    if magic != _MAGIC:
        raise PixelMedRedactionError("Bad PixelMed bridge response magic")
    raw_count = stream.read(4)
    if len(raw_count) != 4:
        raise PixelMedRedactionError("Truncated PixelMed bridge response")
    frame_count = struct.unpack(">i", raw_count)[0]
    if frame_count < 0:
        raise PixelMedRedactionError("Negative frame count in bridge response")

    frames: List[bytes] = []
    for _ in range(frame_count):
        raw_length = stream.read(4)
        if len(raw_length) != 4:
            raise PixelMedRedactionError("Truncated frame length in bridge response")
        frame_length = struct.unpack(">i", raw_length)[0]
        if frame_length < 0:
            raise PixelMedRedactionError("Negative frame length in bridge response")
        frame = stream.read(frame_length)
        if len(frame) != frame_length:
            raise PixelMedRedactionError("Truncated frame bytes in bridge response")
        frames.append(frame)

    trailing = stream.read(1)
    if trailing:
        raise PixelMedRedactionError("Unexpected trailing bytes in bridge response")
    return frames


def redact_jpeg_frames_with_pixelmed(
    frames: Sequence[bytes],
    frame_rectangles: Sequence[Sequence[Rectangle]],
    *,
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
    java_path: Optional[str] = None,
    javac_path: Optional[str] = None,
    jar_path: Optional[Path] = None,
    java_xmx: Optional[str] = None,
) -> List[bytes]:
    jar = jar_path or resolve_pixelmed_codec_jar()
    java = java_path or _resolve_executable("DEIDECHO_JAVA", "java")
    runtime = resolve_pixelmed_bridge_runtime(javac_path=javac_path, jar_path=jar)
    classpath = runtime["classpath"]
    payload = _write_payload(frames, frame_rectangles)

    cmd = [java]
    if java_xmx:
        cmd.append(f"-Xmx{java_xmx}")
    cmd.extend(["-XX:ActiveProcessorCount=1", "-Xss512k"])
    cmd.extend(["-cp", classpath, "PixelMedRedactionBridge"])

    result = subprocess.run(
        cmd,
        input=payload,
        capture_output=True,
        timeout=timeout_seconds,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        stdout = result.stdout.decode("utf-8", errors="replace").strip()
        detail = stderr or stdout or f"java exited {result.returncode}"
        raise PixelMedRedactionError(f"PixelMed bridge failed: {detail[:1000]}")
    return _read_redacted_frames(result.stdout)


def redact_baseline_jpeg_bytes_pixelmed(
    jpeg_bytes: bytes,
    rectangles: Sequence[Rectangle],
    *,
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
    java_xmx: Optional[str] = None,
) -> bytes:
    return redact_jpeg_frames_with_pixelmed(
        [jpeg_bytes],
        [rectangles],
        timeout_seconds=timeout_seconds,
        java_xmx=java_xmx,
    )[0]


def redact_encapsulated_baseline_jpeg_frames_pixelmed(
    ds,
    frame_rectangles: Sequence[Sequence[Rectangle]],
    *,
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
    frame_batch_size: int = 16,
    java_xmx: Optional[str] = None,
):
    """
    Return a copy of a DICOM dataset with PixelMed-redacted JPEG frames.
    """
    if encapsulate is None or generate_frames is None:
        raise ImportError("pydicom is required for PixelMed DICOM helpers")

    number_of_frames = int(getattr(ds, "NumberOfFrames", 1) or 1)
    if number_of_frames != len(frame_rectangles):
        raise ValueError(
            f"Need {number_of_frames} frame rectangle lists, got {len(frame_rectangles)}"
        )
    frame_batch_size = max(1, int(frame_batch_size))

    redacted_frames: List[bytes] = []
    batch_frames: List[bytes] = []
    batch_rectangles: List[Sequence[Rectangle]] = []
    for frame_index, frame in enumerate(
        generate_frames(ds.PixelData, number_of_frames=number_of_frames)
    ):
        batch_frames.append(frame)
        batch_rectangles.append(frame_rectangles[frame_index])
        if len(batch_frames) >= frame_batch_size:
            redacted_frames.extend(
                redact_jpeg_frames_with_pixelmed(
                    batch_frames,
                    batch_rectangles,
                    timeout_seconds=timeout_seconds,
                    java_xmx=java_xmx,
                )
            )
            batch_frames = []
            batch_rectangles = []

    if batch_frames:
        redacted_frames.extend(
            redact_jpeg_frames_with_pixelmed(
                batch_frames,
                batch_rectangles,
                timeout_seconds=timeout_seconds,
                java_xmx=java_xmx,
            )
        )

    if len(redacted_frames) != number_of_frames:
        raise PixelMedRedactionError(
            f"Expected {number_of_frames} redacted frames, got {len(redacted_frames)}"
        )

    out = ds.copy()
    out.PixelData = encapsulate(redacted_frames)
    out[0x7FE00010].is_undefined_length = True
    return out
