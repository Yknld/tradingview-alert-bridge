import signal
import sys

import objc
from AppKit import (
    NSApp,
    NSApplication,
    NSApplicationActivationPolicyRegular,
    NSBackingStoreBuffered,
    NSColor,
    NSFont,
    NSMakeRect,
    NSStatusWindowLevel,
    NSTextField,
    NSTimer,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable,
    NSWindowStyleMaskTitled,
)
from Foundation import NSObject
from Quartz import NSEvent


WIDTH = 280
HEIGHT = 120
MARGIN = 24


class OverlayController(NSObject):
    def init(self):
        self = objc.super(OverlayController, self).init()
        if self is None:
            return None
        self.window = None
        self.label = None
        self.timer = None
        return self

    def build_window(self):
        screen_frame = NSEvent.mouseLocation()
        # Use the primary screen from the current mouse position.
        # AppKit mouseLocation is in bottom-left coordinates.
        from AppKit import NSScreen

        screen = NSScreen.mainScreen()
        visible = screen.visibleFrame()
        x = visible.origin.x + visible.size.width - WIDTH - MARGIN
        y = visible.origin.y + visible.size.height - HEIGHT - MARGIN

        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(x, y, WIDTH, HEIGHT),
            NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskMiniaturizable,
            NSBackingStoreBuffered,
            False,
        )
        self.window.setTitle_("Mouse Coordinate HUD")
        self.window.setLevel_(NSStatusWindowLevel)
        self.window.setOpaque_(False)
        self.window.setBackgroundColor_(NSColor.colorWithCalibratedWhite_alpha_(0.08, 0.92))
        self.window.setReleasedWhenClosed_(False)

        content = self.window.contentView()
        self.label = NSTextField.alloc().initWithFrame_(NSMakeRect(16, 14, WIDTH - 32, HEIGHT - 28))
        self.label.setBezeled_(False)
        self.label.setDrawsBackground_(False)
        self.label.setEditable_(False)
        self.label.setSelectable_(True)
        self.label.setTextColor_(NSColor.whiteColor())
        self.label.setFont_(NSFont.monospacedSystemFontOfSize_weight_(18, 0))
        self.label.setStringValue_("Starting...")
        content.addSubview_(self.label)

        self.window.makeKeyAndOrderFront_(None)

    def start(self):
        self.build_window()
        self.update_(None)
        self.timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.05,
            self,
            "update:",
            None,
            True,
        )

    def update_(self, _timer):
        from AppKit import NSScreen

        point = NSEvent.mouseLocation()
        screen = NSScreen.mainScreen()
        frame = screen.frame()
        x = int(point.x)
        y_bottom = int(point.y)
        y_top = int(frame.size.height - point.y)
        text = (
            "Live Mouse Coordinates\n"
            f"screen_x: {x}\n"
            f"screen_y_top: {y_top}\n"
            f"screen_y_bottom: {y_bottom}"
        )
        self.label.setStringValue_(text)


def main():
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
    controller = OverlayController.alloc().init()
    controller.start()

    signal.signal(signal.SIGINT, lambda *_: app.terminate_(None))
    app.activateIgnoringOtherApps_(True)
    app.run()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
