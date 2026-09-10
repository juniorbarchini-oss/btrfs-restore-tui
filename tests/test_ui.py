import unittest
from pathlib import Path
from textual.app import App, ComposeResult
from btrfs_restore.ui import ProgressModal, ConfirmRestoreModal
from btrfs_restore.models import RestoreProgress, RestoreItem, ConflictResolution
from btrfs_restore.theme import RETRO_CSS


class ModalButtonTestApp(App):
    CSS = RETRO_CSS

    def compose(self) -> ComposeResult:
        yield from ()


class TestUIModals(unittest.IsolatedAsyncioTestCase):
    async def test_progress_modal_close_button_label(self):
        app = ModalButtonTestApp()
        async with app.run_test(size=(80, 24)) as pilot:
            modal = ProgressModal(title="Testing Restore")
            app.push_screen(modal)
            await pilot.pause()

            btn_done = modal.query_one("#btn-done")
            # Ensure label is not stripped to empty string
            self.assertTrue(len(str(btn_done.label)) > 0)
            self.assertIn("Close", str(btn_done.label))

            # Simulate completion
            modal.update_progress(0, 100, "Done", done=True)
            await pilot.pause()

            self.assertTrue(btn_done.display)
            self.assertTrue(btn_done.has_focus)
            self.assertIn("Close", str(btn_done.label))

    async def test_error_state_is_not_overwritten_by_late_progress(self):
        app = ModalButtonTestApp()
        async with app.run_test(size=(80, 24)) as pilot:
            modal = ProgressModal(title="Restoring files...")
            app.push_screen(modal)
            await pilot.pause()

            st = RestoreProgress(total_files=3, processed_files=2, total_bytes=30,
                                 processed_bytes=20, current_file="Completed with errors",
                                 done=True, error="1/3 file(s) failed:\nb.txt: Permission denied",
                                 failed_files=1)
            modal.update_progress(st.spinner_idx, st.percent, st.current_file,
                                  st.done, st.error, st)
            await pilot.pause()
            spinner = modal.query_one("#progress-spinner")
            self.assertIn("errors", str(spinner.render()).lower())
            self.assertTrue(modal.is_done)

            # a stray trailing non-terminal frame must be ignored
            modal.update_progress(1, 66, "b.txt", False, None, None)
            await pilot.pause()
            fname = modal.query_one("#progress-filename")
            self.assertIn("b.txt", str(fname.render()))
            self.assertIn("Permission denied", str(fname.render()))


    def test_signal_handler_only_unwinds(self):
        from btrfs_restore.ui import _unwind_on_signal
        import signal as _sig
        with self.assertRaises(SystemExit) as e:
            _unwind_on_signal(_sig.SIGTERM, None)
        self.assertEqual(e.exception.code, 128 + int(_sig.SIGTERM))

    async def test_confirm_restore_modal_has_skip(self):
        app = ModalButtonTestApp()
        async with app.run_test(size=(100, 30)) as pilot:
            item = RestoreItem(Path("/s/f"), False, 10, Path("f"))
            result = {}
            modal = ConfirmRestoreModal([item], Path("/home/x"))

            def _catch(r):
                result["r"] = r
            app.push_screen(modal, _catch)
            await pilot.pause()
            self.assertTrue(modal.query("#btn-skip"))
            await pilot.press("s")
            await pilot.pause()
            self.assertEqual(result["r"], ConflictResolution.SKIP)


if __name__ == "__main__":
    unittest.main()
