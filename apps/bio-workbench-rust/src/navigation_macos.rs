//! Receive LaunchServices URL events in the existing native window.
//! Uses Foundation directly: winit does not forward these as command-line args.
use eframe::egui;
use objc2::rc::Retained;
use objc2::{DefinedClass, MainThreadMarker, MainThreadOnly, define_class, msg_send, sel};
use objc2_foundation::{NSAppleEventDescriptor, NSAppleEventManager, NSObject, NSObjectProtocol};
use std::cell::RefCell;
use std::sync::mpsc;

struct UrlState {
    sender: mpsc::Sender<String>,
    context: egui::Context,
}
define_class!(
    #[unsafe(super = NSObject)]
    #[name = "BioWorkbenchUrlReceiver"]
    #[thread_kind = MainThreadOnly]
    #[ivars = UrlState]
    struct UrlReceiver;

    unsafe impl NSObjectProtocol for UrlReceiver {}

    impl UrlReceiver {
        #[unsafe(method(handleOpenURL:withReplyEvent:))]
        fn handle_url(&self, event: &NSAppleEventDescriptor, _reply: &NSAppleEventDescriptor) {
            // keyDirectObject = '----'. Only our exact batch-link grammar is accepted.
            if let Some(descriptor) = event.paramDescriptorForKeyword(u32::from_be_bytes(*b"----"))
                && let Some(value) = descriptor.stringValue()
            {
                let text = value.to_string();
                if super::batch_id(&text).is_some() {
                    let _ = self.ivars().sender.send(String::new());
                    let _ = self.ivars().sender.send(text);
                    self.ivars().context.request_repaint();
                }
            }
        }
    }
);

thread_local! {
    static HANDLER: RefCell<Option<Retained<UrlReceiver>>> = const { RefCell::new(None) };
}

pub(super) fn install(sender: mpsc::Sender<String>, context: egui::Context) {
    let Some(main_thread) = MainThreadMarker::new() else {
        return;
    };
    HANDLER.with(|slot| {
        if slot.borrow().is_some() {
            return;
        }
        let allocated = UrlReceiver::alloc(main_thread).set_ivars(UrlState { sender, context });
        // SAFETY: NSObject's designated initializer initializes this allocated subclass.
        let handler: Retained<UrlReceiver> = unsafe { msg_send![super(allocated), init] };
        let manager = NSAppleEventManager::sharedAppleEventManager();
        // SAFETY: selector exactly matches the two-descriptor Objective-C method above.
        // The retained handler stays alive on the main thread for the event loop lifetime.
        unsafe {
            manager.setEventHandler_andSelector_forEventClass_andEventID(
                &handler,
                sel!(handleOpenURL:withReplyEvent:),
                u32::from_be_bytes(*b"GURL"),
                u32::from_be_bytes(*b"GURL"),
            );
        }
        *slot.borrow_mut() = Some(handler);
    });
}
