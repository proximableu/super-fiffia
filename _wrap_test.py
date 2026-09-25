import faulthandler, threading, time
faulthandler.dump_traceback_later(20, exit=True)
def loop():
    for i in range(40):
        time.sleep(1)
        try:
            with open("/tmp/stack.txt","w") as f:
                faulthandler.dump_traceback(file=f)
        except Exception:
            pass
threading.Thread(target=loop, daemon=True).start()
import sys
sys.argv=["pytest","tests/test_records_service.py::test_check_duplicate_returns_info_on_collision","tests/test_records_service.py::test_check_duplicate_none_when_absent","-q","-p","no:cacheprovider"]
import pytest
pytest.main(sys.argv)
