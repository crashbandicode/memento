// Hide the console window on Windows release builds. Without this a
// black cmd.exe pops up behind the GUI every launch.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    memento_app_lib::run();
}
