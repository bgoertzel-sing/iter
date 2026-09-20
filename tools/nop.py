DESCRIPTION = "Perform no action if task is complete and send was already used to report back results, do not re-send!"

def run():
    """Perform no action; used as a no-op when idle."""
    print("NOP")
    return "SUCCESS"
