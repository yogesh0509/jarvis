"""
Spoken and printed phrasing, as pools of variants rather than fixed
strings.

The problem this solves is narrow and specific: actions.py returned one
hardcoded string per outcome, and the same "Done, sir." was the reply to
minimize, maximize, play/pause, mute and more. Hearing the identical
four syllables for five unrelated actions is the single most robotic
thing about the system -- far more than anything about the voice model
itself. Every reply also ended in "sir", at 100% density, which reads as
a template rather than a butler.

Deliberately straight -- no jokes, no commentary on what you're doing.
The goal is that it stops sounding *generated*, not that it becomes a
character. Roughly half the variants carry "sir" and the rest don't,
and where it appears it isn't always trailing; that alone does most of
the work.

One deliberate exception: the boot-time proactive briefing
(briefing_greeting_*, see actions.morning_briefing) opens with a
time-of-day greeting. "No time-of-day asides" was the original rule
here -- reversed on direct request once "warmer by default" became the
stated direction for tone generally (see DECISIONS.md's "Voice replies
and terminal theming" for the full reasoning). This is the one place
that reversal applies; everywhere else in this file, the original rule
still holds.

pick() never returns the same variant twice in a row for a given key,
which matters more than the pool size -- back-to-back repeats are what
actually gets noticed. Pools are plain tuples and the state is one dict
of small ints, so this costs nothing measurable.

If a wit dial is ever wanted, this is the one file it would live in:
add a second pool per key and switch on a module-level level constant.
Nothing else in the codebase would need to change.
"""

import random

import hud

_last_choice: "dict[str, str]" = {}

PHRASES = {
    # --- app focus / launch -----------------------------------------
    "app_not_registered": (
        'I don\'t have "{app}" registered, sir.',
        'Nothing registered under "{app}".',
        'I don\'t know "{app}", sir -- it\'s not in the registry.',
        '"{app}" isn\'t one I have on file.',
    ),
    "focus_already_here": (
        "Already up on this desktop, sir -- bringing it forward.",
        "It's already here. Bringing it forward.",
        "That one's on this desktop, sir. Front and centre.",
        "Already open here -- raising it now.",
    ),
    "focus_other_desktop": (
        "That's open on another desktop, sir -- opened a fresh one here instead.",
        "There's one on another desktop, so I've started a new one here.",
        "Found it elsewhere, sir. Fresh instance on this desktop.",
    ),
    "focus_launched": (
        "Wasn't running. Fired it up for you.",
        "Not running, sir -- starting it now.",
        "Opening it up.",
        "It wasn't open. Launching, sir.",
    ),
    "focus_launched_no_window": (
        "Told it to open, sir, but I haven't spotted its window yet.",
        "Launch sent. No window from it yet, sir.",
        "It's starting -- nothing on screen from it so far.",
    ),
    # --- window management ------------------------------------------
    "minimize_ok": (
        "Out of your way, sir.",
        "Tucked away.",
        "Minimised.",
        "Gone, sir.",
        "Out of sight.",
    ),
    "minimize_nothing": (
        "That's not open on this desktop, sir -- nothing to minimise.",
        "Nothing of that open here to minimise.",
        "Not on this desktop, sir. Nothing to put away.",
    ),
    "maximize_ok": (
        "Full screen, sir.",
        "Filling the screen now.",
        "There you are.",
        "Maximised.",
    ),
    "maximize_nothing": (
        "That's not open on this desktop, sir -- nothing to maximise.",
        "Nothing of that open here to bring up.",
        "Not on this desktop, sir.",
    ),
    "vscode_no_window": (
        "I don't see a VS Code window open for {repo}, sir.",
        "No VS Code window for {repo} that I can find.",
        "Nothing open in VS Code for {repo}, sir.",
    ),
    "vscode_switching": (
        "Switching to {repo}, sir.",
        "Bringing up {repo}.",
        "That'll be {repo}.",
        "Over to {repo}, sir.",
    ),
    # --- desktops ---------------------------------------------------
    "switch_desktop": (
        "Switching to the {direction} desktop, sir.",
        "Moving {direction}.",
        "The {direction} desktop, sir.",
        "On our way {direction}.",
    ),
    "snap": (
        "Snapped {direction}, sir.",
        "Over to the {direction}.",
        "To the {direction}, sir.",
        "Snapped {direction}.",
    ),
    # --- media / system ---------------------------------------------
    "volume_up": ("Turning it up, sir.", "Up it goes.", "Louder, sir.", "Raising the volume."),
    "volume_down": ("Turning it down, sir.", "Down a little.", "Quieter, sir.", "Bringing it down."),
    # An exact amount was given ("by 10", "to 30") and applied via
    # IAudioEndpointVolume -- {percent} is the resulting level, always
    # spoken back rather than echoing what was asked for, since a
    # relative step or a clamp at 0/100 can make the two differ.
    "volume_at": (
        "{percent} percent, sir.",
        "Volume's at {percent} percent now.",
        "There you go -- {percent} percent.",
        "{percent} percent.",
    ),
    # IAudioEndpointVolume couldn't be reached at all (no default
    # device, or the COM call failed) -- distinct from "no amount was
    # said", which isn't a failure, just the plain tap path.
    "volume_unavailable": (
        "I can't get an exact level right now, sir.",
        "No exact control over the volume right now, sir.",
        "Can't read the volume just now.",
    ),
    "mute": ("Silenced, sir.", "Muted.", "Sound off.", "Quiet now, sir."),
    "play_pause": ("There you go.", "Done, sir.", "Toggled.", "Right away."),
    "next_track": ("Skipping ahead, sir.", "Next one.", "Moving on.", "Ahead we go."),
    "prev_track": ("Going back a track, sir.", "Back one.", "Previous track, sir.", "Rewinding."),
    "lock": ("Locking up, sir.", "Locking the machine.", "Securing it now, sir."),
    # --- counts -----------------------------------------------------
    "count_none": ("None open right now, sir.", "Nothing open at the moment.", "Not one, sir."),
    "count_one": ("One window open, sir.", "Just the one.", "A single window, sir."),
    "count_many": ("{count} windows open, sir.", "{count} of them.", "{count} windows, sir."),
    "screen_unknown": (
        "I can't tell what's in focus right now, sir.",
        "Nothing I can make out has focus at the moment.",
        "I'm not seeing a usable window in focus, sir.",
    ),
    "screen_summary_known": (
        'You\'re on {app}, sir -- "{title}", on {monitor}.',
        'That has your attention: {app}, "{title}", on {monitor}.',
        'That\'s {app}, sir: "{title}", on {monitor}.',
    ),
    "screen_summary_unknown": (
        'Something called "{title}" has focus, sir -- on {monitor}, though I don\'t have it registered.',
        'I see "{title}" in focus, on {monitor} -- not one of my registered apps, sir.',
        'You\'re looking at "{title}", sir, on {monitor} -- unregistered, so I can\'t say much more.',
    ),
    "screen_other_summary": (
        "Over on {monitor}: {summary}.",
        "Meanwhile, {monitor} has {summary}.",
        "And on {monitor}, {summary}.",
    ),
    "screen_named_summary": (
        "Over on {monitor}: {summary}.",
        "On {monitor}, sir: {summary}.",
        "That's {monitor} for you: {summary}.",
    ),
    "screen_empty": (
        "Nothing open on {monitor} right now, sir.",
        "Nothing's showing on {monitor} at the moment.",
        "I don't see anything on {monitor}, sir.",
    ),
    "screen_unnamed": (
        "I can't tell which screen you mean, sir -- try 'laptop' or 'monitor'.",
        "That screen doesn't have a name I recognise, sir -- laptop or monitor works, though.",
        "I don't have a name for that one, sir. Laptop, or monitor?",
    ),
    "screen_ambiguous": (
        "I'm not sure which screen you mean, sir -- try naming it, like 'the monitor' or 'my laptop'.",
        "There's more than one 'other' screen, sir -- could you be specific?",
        "Which one, sir? Name it and I'll have a look.",
    ),
    "screen_single": (
        "There's only the one screen, sir.",
        "Just the one screen here, sir -- nowhere else to look.",
        "I've only got the one screen to report on, sir.",
    ),
    "now_playing_track": (
        "That's {track}, sir.",
        "You've got {track} on, sir.",
        "Playing now: {track}.",
    ),
    "now_playing_unknown": (
        "Spotify's open, sir, but I can't tell what's queued up.",
        "It's open, but nothing seems to be playing right now, sir.",
        "Spotify's up, sir, but it doesn't look like anything's playing.",
    ),
    "now_playing_not_open": (
        "Spotify's not open, sir.",
        "I don't see Spotify running, sir.",
        "Nothing's playing -- Spotify isn't open, sir.",
    ),
    "now_playing_no_spotify": (
        "I don't have Spotify registered, sir.",
        "Spotify isn't something I have on file, sir.",
    ),
    "move_monitor_ok": (
        "Over on {monitor} now, sir.",
        "Sent it to {monitor}.",
        "There it is, on {monitor}, sir.",
    ),
    "move_monitor_already_there": (
        "That's already on {monitor}, sir.",
        "It's already there -- on {monitor}.",
        "Nowhere to send it, sir -- it's on {monitor} already.",
    ),
    "move_monitor_unnamed": (
        "I can't tell which screen you mean, sir -- try 'laptop' or 'monitor'.",
        "That screen doesn't have a name I recognise, sir -- laptop or monitor works, though.",
        "I don't have a name for that one, sir. Laptop, or monitor?",
    ),
    "move_monitor_unclear": (
        "I'm not sure which screen you mean, sir -- try naming it, like 'the monitor' or 'my laptop'.",
        "Which screen, sir? Name it and I'll send it over.",
        "Not sure where to send it, sir -- laptop, or monitor?",
    ),
    "move_monitor_single": (
        "There's only the one screen, sir -- nowhere else to send it.",
        "Just the one screen here, sir.",
        "Nowhere else to put it, sir -- only the one screen.",
    ),
    "move_monitor_minimized": (
        "That one's minimised, sir -- nothing to move.",
        "It's tucked away already, sir -- I'd need it visible first.",
        "Can't move a minimised window, sir.",
    ),
    "move_monitor_failed": (
        "That didn't take, sir -- it's still where it was.",
        "Something went wrong there, sir -- it didn't move.",
        "No luck moving that one, sir.",
    ),
    "move_monitor_no_window": (
        "I can't tell what's in focus right now, sir.",
        "Nothing I can make out has focus at the moment.",
        "I'm not seeing a usable window in focus, sir.",
    ),
    # --- conversation / meta ----------------------------------------
    "session_open": ("Yes, sir?", "Sir?", "Go ahead, sir.", "Listening.", "With you, sir."),
    "thinking": (
        "One moment, sir.",
        "Working on it.",
        "Let me think.",
        "Give me a second, sir.",
        "Checking now.",
    ),
    "no_match": (
        "I'm not sure how to do that yet, sir.",
        "That's outside what I can do so far.",
        "I don't have anything for that yet, sir.",
        "Not something I know how to do.",
    ),
    "unknown_action": (
        "I understood that as something to do, sir, but I'm not sure how to actually do it yet.",
        "I follow what you mean, sir -- I just don't have a way to do it.",
        "That parses, but there's nothing wired up behind it yet, sir.",
    ),
    "off_duty": (
        "I'm off duty right now, sir.",
        "Mic's off at the moment, sir.",
        "Standing down right now -- you'll have to bring me back first.",
    ),
    "desktop_number_declined": (
        "I can only switch to the next or previous desktop for now, sir, not a specific number.",
        "Next or previous only, sir -- I can't jump to a numbered desktop yet.",
        "I don't do numbered desktops yet, sir. Next and previous, though.",
    ),
    "declined_question": (
        "That sounded like a question, sir, not a command -- I've left it alone.",
        "That read as a question to me, sir, so I didn't act on it.",
        "Sounded like you were asking, not telling, sir. Nothing done.",
    ),
    "move_window_declined": (
        "I can't move a window to another desktop yet, sir -- only switch which one you're viewing.",
        "Moving a window between desktops isn't something I can do yet, sir.",
        "I don't have a way to send a window to another desktop yet, sir -- only switch to it.",
    ),
    "dictation_start": (
        'Go ahead, sir -- typing whatever you say. Say "stop dictation" when you\'re done.',
        'Dictating now, sir. "Stop dictation" when you\'ve finished.',
        'Ready when you are -- everything goes to the keyboard until you say "stop dictation".',
    ),
    "dictation_stop": ("Ending dictation, sir.", "Dictation off.", "Stopping there, sir."),
    "dictation_declined": (
        'Say "hey jarvis" first, sir -- dictation needs an open conversation.',
        "I'd need an open conversation for that, sir.",
    ),
    "session_close": (
        "Going quiet, sir.",
        "Standing by.",
        "I'll be here, sir.",
        "Closing up.",
    ),
    # Spoken when a conversation closes itself after the idle timeout,
    # rather than on a closing phrase. Distinct from "session_close" so
    # it can say *why* it closed -- and spoken at all because the guide
    # promises every state change is, and this edge was silent (earcon
    # only) despite being on the documented state diagram.
    "session_timeout": (
        "Quiet for a while, sir -- I'll close up.",
        "Nothing for a few minutes, sir. Closing the conversation.",
        "I'll stand by, sir -- say \"hey jarvis\" when you need me.",
    ),
    "pause": ("Going quiet, sir.", "Mic off, sir.", "Standing down."),
    "resume": ("Back online, sir.", "Listening again.", "Back with you, sir."),
    # "wake up"/"start listening" are ordinary things to say mid-
    # conversation, and they're seeds for enable_listening -- so they
    # used to fire a full resume (spoken "Back online, sir.", HUD slammed
    # to ARMED) when nothing was paused. Acknowledge instead.
    "already_listening": (
        "Already listening, sir.",
        "I'm right here, sir.",
        "Never stopped, sir.",
    ),
    "sleep": (
        "Easing into low power mode, sir.",
        "Powering down the heavy circuits, sir.",
        "Going into low power. Say the word whenever.",
    ),
    "wake_up": ("Back at full power, sir.", "Full power, sir.", "Everything's back up."),
    "boot": (
        "Online and at your service, sir.",
        "All systems nominal, sir.",
        "Up and running, sir.",
    ),
    "shutdown": ("Powering down. Goodbye, sir.", "Shutting down, sir.", "Goodbye, sir."),
    "stumbled": (
        "Sorry, sir, I stumbled on that one.",
        "Something went wrong there, sir.",
        "That didn't go to plan, sir. Picking back up.",
    ),
    "audio_trouble": (
        "Couldn't get a word out there, sir -- audio output seems to be having trouble.",
        "Something's wrong with the audio output, sir.",
    ),
    # One-shot boot diagnostic (main.py) -- the mic's software input
    # level measured high enough to plausibly be clipping speech.
    # {percent} is the measured level, not a guess.
    "input_level_high": (
        "Sir, your microphone input level looks quite high, around {percent} percent -- that can clip speech and come through as garbled words.",
        "Your mic input is sitting around {percent} percent, sir -- worth turning it down a little if I keep mishearing you.",
    ),
    # --- timers -------------------------------------------------------
    "timer_set": (
        "{duration} on the clock, sir.",
        "Timer set for {duration}.",
        "You've got {duration}, sir.",
        "{duration}, starting now.",
    ),
    "timer_no_duration": (
        "I didn't catch how long, sir -- try an amount of time, like 'ten minutes'.",
        "How long should I set it for, sir?",
        "Didn't catch a duration there, sir.",
    ),
    "timer_done_unlabeled": (
        "Time's up, sir.",
        "That's your timer, sir.",
        "Timer's done.",
    ),
    "timer_done_labeled": (
        "Reminder, sir: {label}.",
        "Time to {label}, sir.",
        "Sir -- {label}.",
    ),
    "timer_overdue_prefix": (
        "Sorry for the delay, sir --",
        "This one's a bit late, sir --",
    ),
    "timer_cancelled": (
        "Cancelled, sir.",
        "Timer's off.",
        "Done -- that timer's cancelled, sir.",
    ),
    "timer_none_to_cancel": (
        "No timer running, sir.",
        "Nothing to cancel, sir.",
        "There's nothing set, sir.",
    ),
    "timer_list_none": (
        "No timers running, sir.",
        "Nothing on the clock, sir.",
    ),
    "timer_list_one": (
        "{duration} left, sir.",
        "{duration} remaining on your timer, sir.",
    ),
    "timer_list_many": (
        "{count} timers running, sir -- the next one's in {duration}.",
        "{count} on the clock, sir; soonest is {duration} out.",
    ),
    # --- system status --------------------------------------------
    "system_status": (
        "{summary}, sir.",
        "Sir -- {summary}.",
        "{summary}.",
    ),
    "system_status_no_battery": (
        "No battery detected, sir -- looks like a desktop.",
        "I'm not finding a battery to report on, sir.",
    ),
    # --- proactive briefing (boot only, see actions.morning_briefing) --
    "briefing_greeting_morning": (
        "Good morning, sir.",
        "Morning, sir.",
        "Good morning.",
    ),
    "briefing_greeting_afternoon": (
        "Good afternoon, sir.",
        "Afternoon.",
        "Good afternoon to you, sir.",
    ),
    "briefing_greeting_evening": (
        "Good evening, sir.",
        "Evening, sir.",
        "Good evening.",
    ),
    "briefing_greeting_night": (
        "Still up, sir?",
        "Online, sir -- burning the midnight oil.",
        "Evening, sir. Late one.",
    ),
    # {summary} is a noun phrase of varying number ("VS Code" / "three
    # Chrome windows and VS Code") -- every variant below is built
    # around a verb that doesn't need to agree with it ("got", "is"
    # only ever following "there's"), on purpose, rather than risking
    # "{summary} are/is open" picking the wrong one.
    "briefing_layout_current": (
        "You've got {summary} open, sir.",
        "There's {summary} open right now.",
        "Sir, you've got {summary} open at the moment.",
    ),
    "briefing_layout_unchanged": (
        "You've still got {summary} open, sir -- right where you left them.",
        "Still got {summary} open, exactly where you left them.",
        "Sir, {summary} -- still open, right where you left them.",
    ),
    # --- screenshots --------------------------------------------------
    "snip_tool_opened": (
        "Go ahead and select the area, sir.",
        "There you go -- select what you'd like, sir.",
        "Ready when you are, sir.",
    ),
    "screenshot_saved": (
        "Saved, sir.",
        "Got it -- saved to your screenshots, sir.",
        "There you go, sir.",
    ),
    "screenshot_failed": (
        "Couldn't save that, sir.",
        "That didn't save, sir -- something went wrong.",
    ),
    "screenshot_no_window": (
        "I'm not seeing a usable window in focus, sir.",
        "Nothing I can make out has focus at the moment.",
    ),
    # --- social (main.py, meta intents -- see intent_matcher.py) -------
    "social_thanks_reply": (
        "You're welcome, sir.",
        "Any time, sir.",
        "Of course.",
        "My pleasure, sir.",
    ),
    "social_ack_reply": (
        "Very good, sir.",
        "Right you are.",
        "Understood, sir.",
    ),
}


def pick(key: str, **fmt: object) -> str:
    """
    A variant for `key`, never the same one twice running. Unknown keys
    return an empty string rather than raising -- a missing phrase
    should degrade to silence, not take down the action that was
    already carried out successfully.

    That silence used to be completely untraceable -- a typo'd key
    (test_actions_only_references_keys_that_exist and
    test_main_only_references_keys_that_exist now catch every *known*
    call site, but not a new one added without a matching pool) made an
    action that had just genuinely succeeded say nothing at all, with
    no record anywhere that anything had gone wrong. Noted via hud.note
    (debug-only, like every other internal aside in this codebase) --
    not louder than that: the silence itself stays exactly as designed,
    this only makes it possible to find *why* after the fact.
    """
    options = PHRASES.get(key)
    if not options:
        hud.note(f"phrases.pick: no such key {key!r}")
        return ""
    if len(options) > 1:
        previous = _last_choice.get(key)
        candidates = [o for o in options if o != previous] or list(options)
        choice = random.choice(candidates)
    else:
        choice = options[0]
    _last_choice[key] = choice
    return choice.format(**fmt) if fmt else choice
