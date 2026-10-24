namespace AgentProxy
{
    internal enum EngineState
    {
        Starting,
        Running,
        PausedForCharles,
        ProxyNotListening,
        NotResponding,
        PortBusy,
        Crashed,
        Stopped
    }
}
