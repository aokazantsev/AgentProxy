using System;
using System.Collections.Generic;
using System.Web.Script.Serialization;

namespace AgentProxy
{
    internal sealed class EngineCliResult
    {
        public int ExitCode;
        public string Output;
        public string Errors;
        public string Problem;

        public bool Succeeded
        {
            get { return Problem == null && ExitCode == 0; }
        }

        public Dictionary<string, object> Json()
        {
            try
            {
                return new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(Output ?? "");
            }
            catch (ArgumentException)
            {
                return null;
            }
            catch (InvalidOperationException)
            {
                return null;
            }
        }

        public string Describe()
        {
            if (Problem != null) return Problem;
            return "код " + ExitCode + ": " + ((Output ?? "") + " " + (Errors ?? "")).Trim();
        }
    }
}
