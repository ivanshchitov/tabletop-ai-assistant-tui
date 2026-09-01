#!/usr/bin/env python3

"""Точка входа Tabletop AI Assistant."""

from ui.tui_app import TabletopAITUI


def main() -> None:
    app = TabletopAITUI()
    app.run()


if __name__ == "__main__":
    main()
