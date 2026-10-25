namespace AgentProxy.Tests
{
    internal sealed class QuietForm : System.Windows.Forms.Form
    {
        protected override bool ShowWithoutActivation
        {
            get { return true; }
        }
    }
}
