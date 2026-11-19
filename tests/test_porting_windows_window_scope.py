from types import SimpleNamespace

from recheck.porting_desktop import WindowsBackend


def test_windows_resolve_only_handles_owned_by_target_and_children():
    window_owners = {11: 101, 12: 102, 13: 300, 14: 102, 15: None}
    resolved = []

    def enumerate_windows(callback, context):
        for handle in window_owners:
            assert callback(handle, context)
        window_owners[14] = 300  # The window was destroyed and its handle reused.

    def resolve_window(handle):
        resolved.append(handle)
        return SimpleNamespace(handle=handle, element_info=SimpleNamespace(process_id=window_owners[handle]))

    backend = WindowsBackend({101, 102}, enumerate_windows, window_owners.get, resolve_window)

    assert [window.handle for window in backend.windows()] == [11, 12]
    assert resolved == [11, 12]


def test_windows_skip_disappearing_window_and_recheck_uia_owner():
    def enumerate_windows(callback, context):
        for handle in (11, 12, 13):
            assert callback(handle, context)

    changed = SimpleNamespace(handle=12, element_info=SimpleNamespace(process_id=300))
    anchor = SimpleNamespace(handle=13, element_info=SimpleNamespace(process_id=101))
    resolved = {11: None, 12: changed, 13: anchor}
    backend = WindowsBackend({101}, enumerate_windows, lambda _: 101, resolved.get)

    assert backend.windows() == [anchor]
