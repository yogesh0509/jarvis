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

Deliberately straight -- no jokes, no time-of-day asides, no commentary
on what you're doing. The goal is that it stops sounding *generated*,
not that it becomes a character. Roughly half the variants carry "sir"
and the rest don't, and where it appears it isn't always trailing; that
alone does most of the work.

pick() never returns the same variant twice in a row for a given key,
which matters more than the pool size -- back-to-back repeats are what
actually gets noticed. Pools are plain tuples and the state is one dict
of small ints, so this costs nothing measurable.

If a wit dial is ever wanted, this is the one file it would live in:
add a second pool per key and switch on a module-level level constant.
Nothing else in the codebase would need to change.
"""

import random

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
    "mute": ("Silenced, sir.", "Muted.", "Sound off.", "Quiet now, sir."),
    "play_pause": ("There you go.", "Done, sir.", "Toggled.", "Right away."),
    "next_track": ("Skipping ahead, sir.", "Next one.", "Moving on.", "Ahead we go."),
    "prev_track": ("Going back a track, sir.", "Back one.", "Previous track, sir.", "Rewinding."),
    "lock": ("Locking up, sir.", "Locking the machine.", "Securing it now, sir."),
    # --- counts / files ---------------------------------------------
    "count_none": ("None open right now, sir.", "Nothing open at the moment.", "Not one, sir."),
    "count_one": ("One window open, sir.", "Just the one.", "A single window, sir."),
    "count_many": ("{count} windows open, sir.", "{count} of them.", "{count} windows, sir."),
    "explorer_not_focused": (
        "No File Explorer window is focused right now, sir.",
        "Nothing from File Explorer in focus, sir.",
        "I'd need a File Explorer window in focus for that.",
    ),
    "folder_count": ("{count} files in that folder, sir.", "{count} files.", "That's {count} files, sir."),
    "folder_unreadable": (
        "Couldn't read that folder, sir.",
        "That folder won't open for me, sir.",
        "No luck reading that one.",
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
    "pause": ("Going quiet, sir.", "Mic off, sir.", "Standing down."),
    "resume": ("Back online, sir.", "Listening again.", "Back with you, sir."),
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
}


def pick(key: str, **fmt: object) -> str:
    """
    A variant for `key`, never the same one twice running. Unknown keys
    return an empty string rather than raising -- a missing phrase
    should degrade to silence, not take down the action that was
    already carried out successfully.
    """
    options = PHRASES.get(key)
    if not options:
        return ""
    if len(options) > 1:
        previous = _last_choice.get(key)
        candidates = [o for o in options if o != previous] or list(options)
        choice = random.choice(candidates)
    else:
        choice = options[0]
    _last_choice[key] = choice
    return choice.format(**fmt) if fmt else choice
