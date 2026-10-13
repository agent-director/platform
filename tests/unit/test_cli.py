import pytest

from director.main import main


def test_cli_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as e:
        main(["--help"])
    assert e.value.code == 0
    captured = capsys.readouterr()
    assert "Director CLI for Agent Director" in captured.out


def test_cli_run(capsys: pytest.CaptureFixture[str]) -> None:
    main(["run", "test_workflow"])
    captured = capsys.readouterr()
    assert "Running workflow: test_workflow" in captured.out
