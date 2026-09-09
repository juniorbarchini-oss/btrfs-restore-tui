import unittest
from textual.app import App, ComposeResult
from btrfs_restore.ui import ProgressModal
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


if __name__ == "__main__":
    unittest.main()
