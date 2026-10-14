/// Process management for model serving
///
/// TODO:
/// - Spawn subprocess with proper args
/// - Monitor memory and kill if needed
/// - Handle graceful shutdown
/// - Log streaming
/// - Memory guard (OOM protection)
/// - Process resumption from state file
/// - Token streaming response

use std::process::Child;

pub struct ProcessRunner {
    process: Option<Child>,
    model_name: String,
    engine: String,
}

impl ProcessRunner {
    pub fn new(model_name: String, engine: String) -> Self {
        Self {
            process: None,
            model_name,
            engine,
        }
    }

    pub fn launch(&mut self, _extra_args: &str) -> anyhow::Result<()> {
        // TODO: Build command from vllm-runner/runner.py _build_cmd logic
        // - Validate args
        // - Check available memory
        // - Build subprocess command
        // - Spawn with proper environment
        // - Monitor in background
        Ok(())
    }

    pub fn stop(&mut self) -> anyhow::Result<()> {
        // TODO: Send SIGTERM, wait for graceful shutdown
        // TODO: Kill with SIGKILL if needed
        Ok(())
    }

    pub fn status(&self) -> anyhow::Result<ProcessStatus> {
        // TODO: Check if process is running, get PID, uptime
        Ok(ProcessStatus {
            running: false,
            pid: None,
            model: self.model_name.clone(),
            uptime_seconds: 0,
        })
    }
}

#[derive(serde::Serialize)]
pub struct ProcessStatus {
    pub running: bool,
    pub pid: Option<u32>,
    pub model: String,
    pub uptime_seconds: u64,
}
