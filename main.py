from qq_message_manager.qt_font_compat import install_qt_font_compatibility

install_qt_font_compatibility()

from qq_message_manager.app import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
