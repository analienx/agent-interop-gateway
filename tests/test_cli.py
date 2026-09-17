from pathlib import Path

from agent_interop_gateway.cli import _doctor, build_parser


def test_parser_exposes_doctor_command():
    args = build_parser().parse_args(["doctor", "--config", "gateway.toml"])
    assert args.command == "doctor"
    assert args.config == "gateway.toml"


def test_doctor_reports_ready_echo_executor(tmp_path: Path, capsys):
    config = tmp_path / "gateway.toml"
    config.write_text(
        """
[[executors]]
name = "echo"
type = "echo"
capabilities = ["demo"]
""".strip(),
        encoding="utf-8",
    )
    args = build_parser().parse_args(["doctor", "--config", str(config)])
    _doctor(args)
    output = capsys.readouterr().out
    assert '"ready": true' in output
    assert '"type": "echo"' in output
