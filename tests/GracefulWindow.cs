using System;
using System.IO;
using System.Windows.Forms;
public class GracefulWindow {
    private class ProbeForm : Form {
        public string Marker;
        protected override void WndProc(ref Message m) {
            if(m.Msg==0x10 || m.Msg==0x11 || m.Msg==0x16) File.AppendAllText(Marker+".messages",m.Msg.ToString()+"\n");
            base.WndProc(ref m);
        }
    }
    [STAThread] public static void Main(string[] args) {
        AppDomain.CurrentDomain.ProcessExit+=delegate {File.WriteAllText(args[0]+".normal-exit","managed process exit");};
        var form=new ProbeForm { Marker=args[0], Text="CodexZhiru isolated shutdown test", ShowInTaskbar=false, StartPosition=FormStartPosition.Manual, Left=-3000, Top=-3000 };
        var timer=new Timer {Interval=40000}; timer.Tick+=delegate {form.Close();};
        form.Load+=delegate {File.WriteAllText(args[0]+".ready","ready"); timer.Start();};
        form.FormClosing+=delegate(object sender,FormClosingEventArgs e){File.WriteAllText(args[0],e.CloseReason.ToString());};
        Application.Run(form);
    }
}
