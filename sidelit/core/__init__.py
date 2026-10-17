"""The part of Sidelit that decides things.

Nothing in here talks to the compositor, GTK or the file system beyond reading
Claude's own files: the daemon feeds it observations of the desktop and gives it an
actuator to act through. That keeps every rule about what to remember and what to
put back testable against a fake desktop (see tests/).
"""
