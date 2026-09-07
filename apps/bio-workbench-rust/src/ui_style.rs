use eframe::egui::{self, Color32, FontFamily, FontId, Stroke, Vec2};
pub fn configure(ctx: &egui::Context) {
    let mut style = egui::Style {
        wrap_mode: Some(egui::TextWrapMode::Wrap),
        ..Default::default()
    };
    for text in [egui::TextStyle::Body, egui::TextStyle::Button] {
        style
            .text_styles
            .insert(text, FontId::new(11.5, FontFamily::Proportional));
    }
    style
        .text_styles
        .insert(egui::TextStyle::Monospace, FontId::monospace(11.));
    style.text_styles.insert(
        egui::TextStyle::Small,
        FontId::new(10., FontFamily::Proportional),
    );
    style.text_styles.insert(
        egui::TextStyle::Heading,
        FontId::new(13., FontFamily::Proportional),
    );
    style.spacing.item_spacing = Vec2::new(5., 4.);
    style.spacing.button_padding = Vec2::new(7., 3.);
    style.spacing.interact_size = Vec2::new(22., 20.);
    style.spacing.indent = 12.;
    style.visuals = egui::Visuals::dark();
    style.visuals.panel_fill = Color32::from_rgb(49, 51, 54);
    style.visuals.window_fill = Color32::from_rgb(49, 51, 54);
    style.visuals.extreme_bg_color = Color32::from_rgb(26, 28, 30);
    style.visuals.faint_bg_color = Color32::from_rgb(57, 59, 62);
    style.visuals.override_text_color = Some(Color32::from_gray(219));
    style.visuals.selection.bg_fill = Color32::from_rgb(66, 91, 111);
    style.visuals.selection.stroke = Stroke::new(1., Color32::from_rgb(133, 162, 183));
    for visual in [
        &mut style.visuals.widgets.noninteractive,
        &mut style.visuals.widgets.inactive,
        &mut style.visuals.widgets.hovered,
        &mut style.visuals.widgets.active,
        &mut style.visuals.widgets.open,
    ] {
        visual.corner_radius = egui::CornerRadius::ZERO;
        visual.bg_stroke = Stroke::new(1., Color32::from_rgb(80, 82, 86));
    }
    style.visuals.widgets.inactive.bg_fill = Color32::from_rgb(62, 64, 67);
    style.visuals.widgets.inactive.weak_bg_fill = Color32::from_rgb(57, 59, 62);
    style.visuals.widgets.hovered.bg_fill = Color32::from_rgb(78, 88, 96);
    style.visuals.widgets.hovered.weak_bg_fill = Color32::from_rgb(73, 81, 88);
    style.visuals.window_corner_radius = egui::CornerRadius::ZERO;
    style.visuals.menu_corner_radius = egui::CornerRadius::ZERO;
    ctx.set_style(style);
}
