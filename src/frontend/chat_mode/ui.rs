use ratatui::prelude::*;
use ratatui::widgets::{
    HighlightSpacing, List, ListItem, Padding, Scrollbar, ScrollbarOrientation, Wrap,
};
use ratatui::{
    layout::{Constraint, Direction, Layout},
    text::Span,
    widgets::{Block, Borders, Paragraph},
};

use crate::chat::{Chat, ChatState};
use crate::chat_message::ChatMessage;
use crate::frontend::App;

use super::message_formatting::format_chat_message;

pub fn ui(f: &mut ratatui::Frame, area: Rect, app: &mut App) {
    // Create the main layout (vertical)
    let [main_area, help_area] = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Min(0), // Main area
            // Constraint::Length(5), // User input bar (2 lines)
            Constraint::Length(2), // Commands display area
        ])
        .areas(area);

    // Split the main area into two columns
    let [chat_area, chat_list] = Layout::default()
        .direction(Direction::Horizontal)
        .constraints([
            Constraint::Percentage(80), // Left column (chat messages)
            Constraint::Percentage(20), // Right column (other info)
        ])
        .areas(main_area);

    let [chat_messages, input_area] = Layout::default()
        .direction(Direction::Vertical)
        .constraints([Constraint::Min(0), Constraint::Length(8)])
        .spacing(0)
        .areas(chat_area);

    // Render chat messages
    render_chat_messages(f, app, chat_messages);

    // Render other information
    render_chat_list(f, app, chat_list);

    // Render user input bar
    render_input_bar(f, app, input_area);

    // Render commands display area
    render_commands_display(f, app, help_area);
}

fn render_chat_messages(f: &mut ratatui::Frame, app: &mut App, area: Rect) {
    let current_chat = app.current_chat();
    let messages = current_chat.messages.clone();
    let chat_content: Text = messages
        .iter()
        .flat_map(|m| format_chat_message(current_chat, m))
        .collect();

    let num_lines = chat_content.lines.len();

    app.vertical_scroll_state = app.vertical_scroll_state.content_length(num_lines);

    // If we're rendering the current chat and it has new messages
    // set the counter back to 0 and scroll to bottom
    if app.current_chat().new_message_count > 0 {
        app.current_chat_mut().new_message_count = 0;

        let max_height = area.height as usize;

        // If the number of lines is greater than what fits in the chat list area and the vertical
        // there are more lines than where we are scrolled to, scroll down the remaining lines
        if num_lines > max_height && num_lines > app.vertical_scroll {
            app.vertical_scroll = num_lines - max_height;
            app.vertical_scroll_state = app.vertical_scroll_state.position(app.vertical_scroll);
        } else {
            app.vertical_scroll = 0;
        }
    }

    // Unify borders
    let border_set = symbols::border::Set {
        top_right: symbols::line::NORMAL.horizontal_down,
        ..symbols::border::PLAIN
    };

    let message_block = Block::default()
        .title("Chat")
        .border_set(border_set)
        .borders(Borders::TOP | Borders::LEFT | Borders::RIGHT)
        .padding(Padding::horizontal(1));

    #[allow(clippy::cast_possible_truncation)]
    let chat_messages = Paragraph::new(chat_content)
        .block(message_block)
        .wrap(Wrap { trim: false })
        .scroll((app.vertical_scroll as u16, 0));

    f.render_widget(chat_messages, area);

    // Render scrollbar
    f.render_stateful_widget(
        Scrollbar::new(ScrollbarOrientation::VerticalRight)
            .begin_symbol(Some("↑"))
            .end_symbol(Some("↓")),
        area,
        &mut app.vertical_scroll_state,
    );
}
fn render_chat_list(f: &mut ratatui::Frame, app: &mut App, area: Rect) {
    let list: List = app
        .chats
        .iter()
        .map(format_chat_in_list)
        .collect::<List>()
        .highlight_spacing(HighlightSpacing::Always)
        .highlight_style(Style::default().fg(Color::Yellow).bg(Color::DarkGray))
        .block(
            Block::default()
                .title("Chats")
                .borders(Borders::RIGHT | Borders::BOTTOM | Borders::TOP),
        );

    f.render_stateful_widget(list, area, &mut app.chats_state);
}

fn format_chat_in_list(chat: &Chat) -> ListItem {
    let suffix = if chat.is_loading() { " ..." } else { "" };

    let new_message_count = if chat.new_message_count > 0 {
        format!(" ({})", chat.new_message_count)
    } else {
        String::new()
    };

    ListItem::from(format!(
        "{name}{suffix}{new_message_count}",
        name = chat.name
    ))
}

fn render_input_bar(f: &mut ratatui::Frame, app: &mut App, area: Rect) {
    let border_set = symbols::border::Set {
        top_left: symbols::line::NORMAL.vertical_right,
        top_right: symbols::line::NORMAL.vertical_left,
        bottom_right: symbols::line::NORMAL.horizontal_up,
        ..symbols::border::PLAIN
    };

    let block = Block::default()
        .border_set(border_set)
        .padding(Padding::horizontal(1))
        .borders(Borders::ALL);

    if app.current_chat().is_loading() {
        let loading_msg = match &app.current_chat().state {
            ChatState::Loading => "Kwaaking ...".to_string(),
            ChatState::LoadingWithMessage(msg) => format!("Kwaaking ({msg}) ..."),
            ChatState::Ready => unreachable!(),
        };
        let throbber = throbber_widgets_tui::Throbber::default().label(&loading_msg);

        f.render_widget(throbber, block.inner(area));
        return block.render(area, f.buffer_mut());
    }

    // let input = Paragraph::new(app.input.as_str()).block(block);
    app.text_input.set_block(block);
    f.render_widget(&app.text_input, area);
    // Set cursor position
    // f.set_cursor_position(
    //     // Put cursor past the end of the input text
    //     #[allow(clippy::cast_possible_truncation)]
    //     (area.x + app.input.len() as u16 + 1, area.y + 1),
    // );
}

fn render_commands_display(f: &mut ratatui::Frame, app: &App, area: Rect) {
    let commands = Paragraph::new(
        app.supported_commands()
            .iter()
            .map(|c| format!("/{c}"))
            .collect::<Vec<_>>()
            .join(" "),
    )
    .wrap(Wrap { trim: true })
    .block(Block::default().title("Commands").borders(Borders::TOP));
    f.render_widget(commands, area);
}
