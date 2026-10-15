namespace AgentProxy
{
    internal enum EngineState
    {
        Starting,
        Running,
        ProxyNotListening,
        NotResponding,
        PortBusy,
        Crashed,
        Stopped
    }
}
