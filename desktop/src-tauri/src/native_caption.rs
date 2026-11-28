use tauri::WebviewWindow;
use windows_sys::Win32::{
    Foundation::{HWND, LPARAM, LRESULT, POINT, RECT, WPARAM},
    Graphics::Gdi::ScreenToClient,
    UI::{
        HiDpi::GetDpiForWindow,
        Shell::{DefSubclassProc, RemoveWindowSubclass, SetWindowSubclass},
        WindowsAndMessaging::{
            GetClientRect, HTCAPTION, HTCLIENT, HTMAXBUTTON, WM_NCDESTROY, WM_NCHITTEST,
        },
    },
};

use crate::window_frame::{
    RESEARCH_BUTTON_WIDTH, RESEARCH_CAPTION_END, RESEARCH_CAPTION_HEIGHT, RESEARCH_CAPTION_TOP,
    RESEARCH_DRAG_WIDTH,
};

const SUBCLASS_ID: usize = 1;

pub(crate) fn attach(window: &WebviewWindow) -> tauri::Result<()> {
    let view = window.clone();
    window.run_on_main_thread(move || {
        if let Ok(handle) = view.hwnd() {
            // The HWND belongs to this UI thread and the callback holds no Rust state.
            unsafe { SetWindowSubclass(handle.0, Some(caption_subclass), SUBCLASS_ID, 0) };
        }
    })
}

fn caption_hit(point: POINT, client_width: i32, dpi: u32) -> Option<LRESULT> {
    let scale = |units: i32| ((i64::from(units) * i64::from(dpi) + 48) / 96) as i32;
    let top = scale(RESEARCH_CAPTION_TOP);
    let bottom = scale(RESEARCH_CAPTION_TOP + RESEARCH_CAPTION_HEIGHT);
    if dpi == 0 || !(top..bottom).contains(&point.y) {
        return None;
    }

    let maximize_left = client_width - scale(RESEARCH_CAPTION_END + 2 * RESEARCH_BUTTON_WIDTH);
    let maximize_right = client_width - scale(RESEARCH_CAPTION_END + RESEARCH_BUTTON_WIDTH);
    let drag_left = client_width
        - scale(RESEARCH_CAPTION_END + 3 * RESEARCH_BUTTON_WIDTH + RESEARCH_DRAG_WIDTH);
    let drag_right = client_width - scale(RESEARCH_CAPTION_END + 3 * RESEARCH_BUTTON_WIDTH);
    if (maximize_left..maximize_right).contains(&point.x) {
        Some(HTMAXBUTTON as LRESULT)
    } else if (drag_left..drag_right).contains(&point.x) {
        Some(HTCAPTION as LRESULT)
    } else {
        None
    }
}

unsafe extern "system" fn caption_subclass(
    hwnd: HWND,
    message: u32,
    wparam: WPARAM,
    lparam: LPARAM,
    id: usize,
    _reference: usize,
) -> LRESULT {
    if message == WM_NCDESTROY {
        unsafe { RemoveWindowSubclass(hwnd, Some(caption_subclass), id) };
    }
    let default = unsafe { DefSubclassProc(hwnd, message, wparam, lparam) };
    // Preserve Tao's resize borders. Only the two reserved zones replace a client hit.
    if message != WM_NCHITTEST || default != HTCLIENT as LRESULT {
        return default;
    }
    // Screen positions can be negative on monitors to the left or above the primary one.
    let mut point = POINT {
        x: (lparam as u16 as i16).into(),
        y: ((lparam >> 16) as u16 as i16).into(),
    };
    let mut client: RECT = unsafe { std::mem::zeroed() };
    if unsafe { ScreenToClient(hwnd, &mut point) } == 0
        || unsafe { GetClientRect(hwnd, &mut client) } == 0
    {
        return default;
    }
    caption_hit(point, client.right, unsafe { GetDpiForWindow(hwnd) }).unwrap_or(default)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_drag_and_maximize_zones_intercept_client_at_normal_and_scaled_dpi() {
        for (dpi, width, y, drag, min, max, close) in [
            (96, 640, 28, 480, 520, 566, 612),
            (192, 1280, 56, 960, 1040, 1132, 1224),
        ] {
            assert_eq!(
                caption_hit(POINT { x: drag, y }, width, dpi),
                Some(HTCAPTION as LRESULT)
            );
            assert_eq!(
                caption_hit(POINT { x: max, y }, width, dpi),
                Some(HTMAXBUTTON as LRESULT)
            );
            for x in [0, min, close] {
                assert_eq!(caption_hit(POINT { x, y }, width, dpi), None);
            }
            assert_eq!(caption_hit(POINT { x: max, y: 0 }, width, dpi), None);
        }
    }
}
