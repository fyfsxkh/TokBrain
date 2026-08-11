from pathlib import Path

from app.services import temp_files


def test_unlink_retries_a_windows_sharing_violation(monkeypatch, tmp_path):
    target = tmp_path / "work.asr.wav"
    target.write_bytes(b"audio")
    original_unlink = Path.unlink
    calls = 0

    def flaky_unlink(path: Path, *, missing_ok: bool = False):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise PermissionError(13, "file is in use", str(path))
        return original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    monkeypatch.setattr(temp_files.time, "sleep", lambda _seconds: None)

    assert temp_files.unlink_with_retries(target, attempts=3)
    assert calls == 3
    assert not target.exists()


def test_cleanup_only_removes_regenerated_media(tmp_path):
    stale_wav = tmp_path / "old.asr.wav"
    stale_opus = tmp_path / "old.asr.opus"
    stale_restricted_audio = tmp_path / "old.restricted-audio"
    stale_video = tmp_path / "old.mp4"
    keep = tmp_path / "keep.txt"
    for path in (stale_wav, stale_opus, stale_restricted_audio, stale_video, keep):
        path.write_bytes(b"temporary")

    assert temp_files.cleanup_stale_temp_media(tmp_path) == 4
    assert not stale_wav.exists()
    assert not stale_opus.exists()
    assert not stale_restricted_audio.exists()
    assert not stale_video.exists()
    assert keep.exists()
