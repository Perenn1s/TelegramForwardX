"""Keep the first error and periodic summaries without flooding the console."""

import logging
import time
from collections import OrderedDict
from logging.handlers import RotatingFileHandler


class RepeatFilter(logging.Filter):
    def __init__(self, interval=30, clock=time.monotonic):
        super().__init__()
        self.interval = interval
        self.clock = clock
        self.seen = OrderedDict()

    def filter(self, record):
        if record.levelno < logging.WARNING:
            return True
        key = (record.name, record.levelno, str(record.msg))
        now = self.clock()
        previous, count = self.seen.get(key, (now - self.interval, 0))
        if now - previous < self.interval:
            self.seen[key] = (previous, count + 1)
            return False
        self.seen[key] = (now, 0)
        self.seen.move_to_end(key)
        if len(self.seen) > 256:
            self.seen.popitem(last=False)
        if count:
            record.msg = record.getMessage() + f" (suppressed {count} repeats in {self.interval}s)"
            record.args = ()
        return True


def configure_logging(log_file=None):
    handlers = [logging.StreamHandler()]
    if log_file:
        handlers.append(RotatingFileHandler(log_file, maxBytes=2 * 1024 * 1024,
                                            backupCount=3, encoding="utf-8"))
    for handler in handlers:
        handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))

    class DispatchHandler(logging.Handler):
        def emit(self, record):
            for output in handlers:
                output.handle(record)

        def close(self):
            for output in handlers:
                output.close()
            super().close()

    # Filter once before both outputs so repeat summaries remain identical.
    dispatcher = DispatchHandler()
    dispatcher.addFilter(RepeatFilter())
    logging.basicConfig(level=logging.INFO, handlers=[dispatcher], force=True)
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("python_socks").setLevel(logging.WARNING)
