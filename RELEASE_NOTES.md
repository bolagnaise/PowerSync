<!-- release: v2.12.1461 -->
## Solar export recovery

If Profit Max cannot apply its solar-export hold and the return to self-consumption also fails, PowerSync now keeps the previous execution status so the next cycle retries recovery. This prevents a failed return to normal operation from being treated as complete; fresh inverter readings are still needed to confirm battery behavior.
