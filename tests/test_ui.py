import unittest
from textual.app import App, ComposeResult
from btrfs_restore.ui import ProgressModal
from btrfs_restore.models import RestoreProgress
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


if __name__ == "__main__":
    unittest.main()
