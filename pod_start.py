#!/usr/bin/env python3
"""
TTS Project Orchestrator (pod mode).
Python-based CLI for multi-container pod management.
"""

import argparse
import os
import sys
import subprocess
import json
import yaml
from pathlib import Path

# --- Configuration ---
TEMPLATE_FILE = Path("pod/tts-dev-pod.yaml.tmpl")
ENV_FILE = Path(".env")
POD_NAME = "tts-dev"

def get_runtime():
    """Resolve the container runtime: TTS_RUNTIME env -> podman -> docker."""
    runtime = os.environ.get("TTS_RUNTIME")
    if runtime in ["podman", "docker"]:
        return runtime

    for cmd in ["podman", "docker"]:
        try:
            subprocess.run([cmd, "--version"], capture_output=True, check=True)
            return cmd
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue
    
    print("ERROR: No container runtime (podman or docker) found.", file=sys.stderr)
    sys.exit(1)

RUNTIME_BIN = get_runtime()

def load_env():
    """Simple .env loader for host-specific variables."""
    if ENV_FILE.exists():
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    os.environ[key.strip()] = value.strip()
    else:
        print(f"Note: {ENV_FILE} not found. Using defaults.")

def resolve_app_config():
    """
    Ensure all necessary environment variables for the pod template are set.
    Fallback: .env -> .env.example -> ~/.cache/tts-models
    """
    load_env()

    # 1. Resolve TTS_APP_DIR
    default_app_dir = str(Path(__file__).resolve().parent)
    current_app_dir = os.environ.get("TTS_APP_DIR")
    
    if current_app_dir:
        # Expand ~ before checking if absolute
        expanded_app_dir = Path(current_app_dir).expanduser().resolve()
        os.environ["TTS_APP_DIR"] = str(expanded_app_dir)
    else:
        os.environ["TTS_APP_DIR"] = default_app_dir

    # 2. Resolve TTS_MODELS_ROOT
    models_root = os.environ.get("TTS_MODELS_ROOT")
    if not models_root and Path(".env.example").exists():
        with open(".env.example") as f:
            for line in f:
                if line.startswith("TTS_MODELS_ROOT="):
                    models_root = line.split("=", 1)[1].strip()
                    break
    if not models_root:
        models_root = str(Path.home() / ".cache" / "tts-models")
    
    models_root_path = Path(models_root).expanduser().resolve()
    os.environ["TTS_MODELS_ROOT"] = str(models_root_path)

    # 3. Map specific legacy vars for the template
    legacy_vars = {
        "MODELS_DIR": "models",
        "PIPER_VOICES_DIR": "piper",
        "SUPER_TONIC_CACHE_DIR": "supertonic",
        "REFS_DIR": "refs",
    }

    for var, subfolder in legacy_vars.items():
        if not os.environ.get(var):
            os.environ[var] = str(models_root_path / subfolder)

def detect_backends(pod):
    """
    Detect TTS backends by parsing the pod YAML definition.
    Returns a list of dictionaries matching the UI server's TTS_BACKENDS contract.
    """
    backends = []
    for container in pod['spec']['containers']:
        labels = container.get('labels', {})
        if labels.get('tts.backend') != 'true':
            continue
        ports = container.get('ports', [])
        if not ports:
            continue
        port = ports[0].get('containerPort')
        if not port:
            continue

        entry = {
            "label": container['name'],
            "url": f"http://127.0.0.1:{port}/v1/audio/speech",
        }

        # Optional per-backend response format (e.g. audio8 only accepts wav)
        fmt = labels.get('tts.format')
        if fmt:
            entry['format'] = fmt

        # GPU: explicit label takes precedence, falls back to TTS_AUTO_EGPU env var
        is_gpu = labels.get('tts.gpu') == 'true'
        if not is_gpu:
            for env in container.get('env', []):
                if env.get('name') == 'TTS_AUTO_EGPU' and env.get('value') == '1':
                    is_gpu = True
                    break
        entry['gpu'] = is_gpu

        # Optional per-backend instructions box
        if labels.get('tts.instructions') == 'true':
            entry['instructions'] = True

        # Optional per-backend model routing
        if labels.get('tts.send_model') == 'false':
            entry['send_model'] = False

        backends.append(entry)
    return backends

def build_manifest():
    """Build the final manifest from template + env + backends."""
    resolve_app_config()

    # If template doesn't exist, try the standard file (for compatibility)
    source_file = TEMPLATE_FILE if TEMPLATE_FILE.exists() else Path("tts-dev-pod.yaml")
    if not source_file.exists():
        sys.exit(f"Error: {TEMPLATE_FILE} or tts-dev-pod.yaml not found.")
        
    with open(source_file) as f:
        template = f.read()
        
    # 1. Replace environment variables (only if using template)
    if TEMPLATE_FILE.exists():
        for key, value in os.environ.items():
            template = template.replace('${' + key + '}', str(value))
            
    # 2. Parse YAML
    pod = yaml.safe_load(template)
    
    # 3. Detect backends from YAML and inject
    backends = detect_backends(pod)
    for container in pod['spec']['containers']:
        if container['name'] == 'ui':
            # Ensure env list exists
            if 'env' not in container:
                container['env'] = []
                
            # Find or add TTS_BACKENDS
            injected = False
            for env in container['env']:
                if env['name'] == 'TTS_BACKENDS':
                    env['value'] = json.dumps(backends)
                    injected = True
            if not injected:
                container['env'].append({"name": "TTS_BACKENDS", "value": json.dumps(backends)})
    
    return yaml.safe_dump(pod, default_flow_style=False)

def run_cmd(cmd, input_data=None):
    """Execute a subprocess command."""
    try:
        subprocess.run(cmd, input=input_data, check=True)
    except subprocess.CalledProcessError as e:
        sys.exit(f"Command failed: {' '.join(cmd)}")

# --- CLI Commands ---

def cmd_start():
    print("Building manifest and starting pod...")
    # Tear down existing pod first (idempotent)
    try:
        subprocess.run([RUNTIME_BIN, 'pod', 'rm', '-f', POD_NAME],
                       capture_output=True, check=True)
    except subprocess.CalledProcessError:
        pass  # pod didn't exist
    manifest = build_manifest()
    run_cmd([RUNTIME_BIN, 'kube', 'play', '-'], manifest.encode())
    print(f"Pod '{POD_NAME}' started.")
    print(f"🚀 First run? Models are downloading in the background.")
    print(f"👉 Run `python3 start_pod.py logs` to watch the progress.")

def cmd_stop():
    run_cmd([RUNTIME_BIN, 'pod', 'rm', '-f', POD_NAME])
    print(f"Pod '{POD_NAME}' stopped.")

def cmd_status():
    run_cmd([RUNTIME_BIN, 'ps', '-a'])

def cmd_logs():
    run_cmd([RUNTIME_BIN, 'pod', 'logs', '-f', POD_NAME])

def cmd_restart():
    cmd_stop()
    cmd_start()

# --- Main ---

def main():
    parser = argparse.ArgumentParser(description="TTS Project Orchestrator")
    subparsers = parser.add_subparsers(dest='command')
    
    subparsers.add_parser('start', help='Start the TTS stack')
    subparsers.add_parser('stop', help='Stop the TTS stack')
    subparsers.add_parser('status', help='Show container status')
    subparsers.add_parser('logs', help='Show pod logs')
    subparsers.add_parser('restart', help='Restart the TTS stack')
    
    args = parser.parse_args()
    
    commands = {
        'start': cmd_start,
        'stop': cmd_stop,
        'status': cmd_status,
        'logs': cmd_logs,
        'restart': cmd_restart,
    }
    
    if args.command in commands:
        commands[args.command]()
    else:
        parser.print_help()

if __name__ == '__main__':
    main()
