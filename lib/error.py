class TestRunInitError(Exception):
    def __init__(self, *args, **kwargs):
        super(TestRunInitError, self).__init__(*args, **kwargs)
