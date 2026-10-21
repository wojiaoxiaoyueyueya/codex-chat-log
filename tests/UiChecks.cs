using System;
using System.Collections;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using System.Threading;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Documents;
using System.Windows.Media;
using System.Windows.Threading;
using CodexZhiru;

public class UiChecks {
    static int assertions;
    static void Check(bool condition,string name) {if(!condition) throw new Exception(name); assertions++;}
    static IEnumerable<DependencyObject> Walk(DependencyObject node) {
        yield return node; IEnumerable children=null;
        if(node is FlowDocument) children=((FlowDocument)node).Blocks;
        else if(node is Paragraph) children=((Paragraph)node).Inlines;
        else if(node is Span) children=((Span)node).Inlines;
        else if(node is Section) children=((Section)node).Blocks;
        else if(node is System.Windows.Documents.List) children=((System.Windows.Documents.List)node).ListItems;
        else if(node is ListItem) children=((ListItem)node).Blocks;
        else if(node is Table) children=((Table)node).RowGroups;
        else if(node is TableRowGroup) children=((TableRowGroup)node).Rows;
        else if(node is TableRow) children=((TableRow)node).Cells;
        else if(node is TableCell) children=((TableCell)node).Blocks;
        else if(node is BlockUIContainer) children=new[]{((BlockUIContainer)node).Child};
        else if(node is Decorator) children=new[]{((Decorator)node).Child};
        else if(node is Panel) children=((Panel)node).Children;
        if(children!=null) foreach(object child in children) if(child is DependencyObject) foreach(var descendant in Walk((DependencyObject)child)) yield return descendant;
    }
    static object Field(MainWindow w,string name) {return typeof(MainWindow).GetField(name,BindingFlags.Instance|BindingFlags.NonPublic).GetValue(w);}
    static object Call(MainWindow w,string name,params object[] args) {return typeof(MainWindow).GetMethod(name,BindingFlags.Instance|BindingFlags.NonPublic).Invoke(w,args);}
    static void Set(MainWindow w,string name,object value) {typeof(MainWindow).GetField(name,BindingFlags.Instance|BindingFlags.NonPublic).SetValue(w,value);}
    static Dictionary<string,object> Result(string title) {
        return new Dictionary<string,object> {
            {"chat",new Dictionary<string,object>{{"title",title},{"project","fixture"},{"parent_id",""},{"archived",false}}},
            {"messages",new object[]{new Dictionary<string,object>{{"role","assistant"},{"text",title},{"time",""}}}},
            {"start",0},{"total",1}};
    }
    static Dictionary<string,object> ChangeRow(string id,bool helper,string owner,bool selectable,int helperCount) {
        return new Dictionary<string,object>{{"id",id},{"title",id},{"project","fixture"},{"reason",helper?"随所属聊天自动导入":"新增聊天"},
            {"helper",helper},{"owner_id",owner},{"helper_count",helperCount},{"selectable",selectable},{"status","new"}};
    }
    static void Pump(Task task) {
        if(task.IsCompleted) {task.GetAwaiter().GetResult();return;}
        var frame=new DispatcherFrame();var timeout=new DispatcherTimer {Interval=TimeSpan.FromSeconds(3)};
        timeout.Tick+=delegate {frame.Continue=false;};timeout.Start();
        task.ContinueWith(delegate {Dispatcher.CurrentDispatcher.BeginInvoke(new Action(delegate{frame.Continue=false;}));},TaskScheduler.FromCurrentSynchronizationContext());
        Dispatcher.PushFrame(frame);timeout.Stop();
        if(!task.IsCompleted) throw new Exception("Timed out waiting for preview fixture");
        task.GetAwaiter().GetResult();
    }
    [STAThread] public static int Main() {
        try {
            var app=new Application();SynchronizationContext.SetSynchronizationContext(new DispatcherSynchronizationContext(Dispatcher.CurrentDispatcher));string copied=null;
            string raw="5. 核对虚构备份后保存 `example[0] = 3`。\n\n```text\nexample[0] = 3\n  example_count = 3\nstatus = READY\n**literal code**\n```\n\n示例完成。**整个过程可验证、可回滚。**";
            var doc=RichMessage.Document(raw,delegate(string text){copied=text;});
            var nodes=Walk(doc).ToList();string displayed=new TextRange(doc.ContentStart,doc.ContentEnd).Text;
            Check(!displayed.Contains("```"),"Fence markers must not appear as prose");
            Check(!displayed.Contains("**整个过程"),"Bold markers must not appear as prose");
            Check(nodes.OfType<Span>().Any(s=>s.FontWeight==FontWeights.SemiBold&&new TextRange(s.ContentStart,s.ContentEnd).Text.Contains("整个过程")),"Bold text rendered");
            Check(doc.FontWeight==FontWeights.Normal,"Body stays regular weight");
            Check(nodes.OfType<System.Windows.Documents.List>().Single().StartIndex==5,"Ordered list starts at original number");
            var code=nodes.OfType<TextBox>().Single();
            Check(code.Text.Contains("  example_count = 3"),"Code indentation preserved");
            Check(code.Text.Contains("**literal code**"),"Markdown inside code remains literal");
            nodes.OfType<Button>().Single().RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
            Check(copied==code.Text,"Copy code uses original code, not formatted labels");
            var table=RichMessage.Document("## 核对\n\n| 名称 | 结果 |\n| --- | --- |\n| **hello** | 已完成 |",null);
            var tableNodes=Walk(table).ToList();
            Check(tableNodes.OfType<Table>().Single().RowGroups[0].Rows.Count==2,"Table renders header and row");
            Check(tableNodes.OfType<Paragraph>().Any(p=>p.FontSize==18),"Heading renders at heading size");
            var safe=RichMessage.Document("STATE_RET_OK C:\\Users\\Example\\Documents\n\n<script>not executable</script>\n\n[地址](javascript:example)",delegate(string s){copied=s;});
            string literal=new TextRange(safe.ContentStart,safe.ContentEnd).Text;
            Check(literal.Contains("STATE_RET_OK")&&literal.Contains("C:\\Users\\Example\\Documents"),"Plain technical text preserved");
            Check(literal.Contains("<script>not executable</script>"),"HTML remains inert text");
            Walk(safe).OfType<Hyperlink>().Single().RaiseEvent(new RoutedEventArgs(Hyperlink.ClickEvent));
            Check(copied=="javascript:example","Link action only copies, never launches content");
            var unclosed=RichMessage.Document("```c\nint value = 1;",null);
            Check(Walk(unclosed).OfType<TextBox>().Single().Text=="int value = 1;","Unclosed fence still displays code");
            var quote=RichMessage.Document("> 这是**重点**\n> 第二行",null);
            Check(Walk(quote).OfType<Section>().Any(),"Quote renders as quote section");

            var window=new MainWindow(new[]{"--ui-test"});
            var policy=(Button)Field(window,"backupPolicy");
            Check(!policy.IsEnabled,"Backup policy requires a preview");
            Set(window,"token","synthetic-preview");Call(window,"UpdateSelection");
            Check(policy.IsEnabled&&Convert.ToString(policy.Content).Contains("以备份为准"),"Explicit backup policy button available");
            Set(window,"preferBackup",true);Call(window,"UpdateSelection");
            Check(Convert.ToString(policy.Content).Contains("保留本机"),"Backup policy can switch back");
            string confirmation=(string)Call(window,"ImportConfirmation",3);
            Check(confirmation.Contains("替换为备份")&&confirmation.Contains("回滚副本"),"Backup confirmation explains replacement and rollback");
            Call(window,"Busy",true,"replan");Check(!policy.IsEnabled,"No policy changes during an operation");
            Call(window,"Busy",false,"done");Set(window,"token",null);Set(window,"preferBackup",false);Call(window,"UpdateSelection");
            var rows=(List<Dictionary<string,object>>)Field(window,"chats");
            rows.Add(new Dictionary<string,object>{{"id","one"},{"title","hello"},{"project","项目"},{"project_id","p"},{"parent_id",""},{"archived",false}});
            Call(window,"RenderTree");window.Show();window.UpdateLayout();
            var tree=(TreeView)Field(window,"tree");var group=(TreeViewItem)tree.Items[0];var item=(TreeViewItem)group.Items[0];item.IsSelected=true;window.UpdateLayout();item.ApplyTemplate();
            var row=(Border)item.Template.FindName("Row",item);
            Check(((SolidColorBrush)row.Background).Color.ToString()=="#FFE1F1E7","Selection uses green highlight");
            Call(window,"BeginReading","reading");window.UpdateLayout();
            Check(tree.IsEnabled,"Reading must not disable entire sidebar");
            Check(((SolidColorBrush)row.Background).Color.ToString()=="#FFE1F1E7","Selection stays green during read");
            Check(!((Button)Field(window,"importButton")).IsEnabled,"Writes remain gated during active reads");
            ((TextBox)Field(window,"search")).Focus();window.UpdateLayout();
            Check(((SolidColorBrush)row.Background).Color.ToString()=="#FFE1F1E7","Inactive selection does not turn grey");
            Call(window,"EndReading",new object[]{null});Call(window,"Busy",true,"write");
            Check(!tree.IsEnabled,"Before the frozen reader is ready live reads remain blocked");
            Set(window,"browseReady",true);Set(window,"home","local");Set(window,"archiveHome","backup");
            Set(window,"browseHome","frozen-local");Set(window,"browseArchiveHome","frozen-backup");
            Call(window,"UpdateInteractive");
            Check(tree.IsEnabled&&((TreeView)Field(window,"differences")).IsEnabled,"Long operations keep navigation enabled after snapshot preparation");
            Check((string)Call(window,"ReadHome","local")=="frozen-local","Local reads during operations use the independent snapshot");
            Check((string)Call(window,"ReadHome","backup")=="frozen-backup","Backup reads during import use a surviving independent snapshot");
            Check(!((Button)Field(window,"importButton")).IsEnabled&&!((Button)Field(window,"exportButton")).IsEnabled,"Concurrent mutations remain blocked while browsing");
            Call(window,"BeginReading","must not replace operation status");
            Check(((TextBlock)Field(window,"status")).Text=="write","Browsing does not replace operation progress");
            Call(window,"EndReading",new object[]{null});
            Call(window,"Busy",false,"ready");window.Close();

            var bound=new MainWindow(new[]{"--ui-test"});
            Set(bound,"token","synthetic-bound-preview");
            Call(bound,"SetImportPlan",new Dictionary<string,object> {
                {"changes",new object[]{ChangeRow("parent",false,"",true,2),ChangeRow("review",true,"parent",true,0),
                    ChangeRow("nested-helper",true,"parent",true,0),ChangeRow("independent",false,"",true,0),
                    ChangeRow("blocked",false,"",false,1),ChangeRow("blocked-helper",true,"blocked",false,0)}},
                {"safe_chats",2},{"safe_helpers",2},{"source_chats",6},{"local_chats",0}});
            var boundRows=(List<Change>)Field(bound,"changes");
            var parent=boundRows.Single(c=>c.Id=="parent");
            Check(boundRows.Where(c=>c.Helper&&c.Selectable).All(c=>c.Selected),"Helpers initially follow selected owner");
            Check(((List<CheckBox>)Field(bound,"changeChecks")).Count==3,"Only ordinary chats have checkboxes, including blocked chat");
            var boundTree=(TreeView)Field(bound,"differences");
            var boundGroup=(TreeViewItem)boundTree.Items[0];
            Check(boundGroup.Items.Count==3,"Helpers never appear as standalone import choices");
            var parentNode=(TreeViewItem)boundGroup.Items[0];
            var helperGroup=(TreeViewItem)parentNode.Items[0];
            Check(helperGroup.Items.Count==2,"Nested helpers and review appear under owning chat");
            Check(helperGroup.Items.Cast<TreeViewItem>().All(n=>!Walk((DependencyObject)n.Header).OfType<CheckBox>().Any()),"Attached records have no checkbox");
            parent.Selected=false;
            Check(boundRows.Where(c=>c.Helper).All(c=>!c.Selected),"Unchecking owner cancels every helper");
            Check(((TextBlock)Field(bound,"selectionSummary")).Text.Contains("自动随附 0"),"Counts update after owner deselection");
            ((Button)Field(bound,"selectAll")).RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
            Check(parent.Selected&&boundRows.Where(c=>c.Helper&&c.Selectable).All(c=>c.Selected),"Select all includes helpers through owners");
            Check(!boundRows.Single(c=>c.Id=="blocked-helper").Selected,"Select all cannot include blocked helper group");
            Check(((string)Call(bound,"ImportConfirmation",2)).Contains("自动随附 2"),"Confirmation counts automatic helpers separately");
            ((Button)Field(bound,"selectNone")).RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
            Check(boundRows.All(c=>!c.Selected)&&!((Button)Field(bound,"applyButton")).IsEnabled,"Cancel all clears owners and helpers and disables import");
            parent.Selected=true;
            Check(boundRows.Where(c=>c.OwnerId=="parent").All(c=>c.Selected)&&!boundRows.Single(c=>c.Id=="independent").Selected,"Reselecting owner binds only its own records");
            bound.Close();

            var pending=new Dictionary<string,TaskCompletionSource<Dictionary<string,object>>>();
            var requestedHomes=new Dictionary<string,string>();
            var race=new MainWindow(new[]{"--ui-test"},delegate(Dictionary<string,object> request) {
                var source=new TaskCompletionSource<Dictionary<string,object>>();pending[(string)request["id"]]=source;requestedHomes[(string)request["id"]]=(string)request["home"];return source.Task;
            });
            race.Show();
            var first=(Task)Call(race,"LoadConversation","fixture","first",-1);
            var second=(Task)Call(race,"LoadConversation","fixture","second",-1);
            Check(((TreeView)Field(race,"tree")).IsEnabled,"Rapid navigation remains enabled");
            pending["second"].SetResult(Result("second-result"));Pump(second);
            pending["first"].SetResult(Result("first-result"));Pump(first);
            Check(((TextBlock)Field(race,"chatTitle")).Text=="second-result","Late result must not replace newer chat");
            Check((int)Field(race,"reading")==0,"Every preview read settles its loading state");
            Set(race,"archiveHome","fixture");
            Set(race,"compareChange",new Change {Id="compare-first",Title="first",Project="fixture",Status="newer"});
            var compareFirst=(Task)Call(race,"LoadComparison",-1);
            Set(race,"compareChange",new Change {Id="compare-second",Title="second",Project="fixture",Status="newer"});
            var compareSecond=(Task)Call(race,"LoadComparison",-1);
            pending["compare-second"].SetResult(Result("second-comparison"));Pump(compareSecond);
            pending["compare-first"].SetResult(Result("first-comparison"));Pump(compareFirst);
            var shown=Walk((StackPanel)Field(race,"compareMessages")).OfType<RichTextBox>().Single().Document;
            Check(new TextRange(shown.ContentStart,shown.ContentEnd).Text.Contains("second-comparison"),"Comparison ignores stale result");
            Check(((TreeView)Field(race,"differences")).IsEnabled,"Comparison list stays enabled");
            Set(race,"home","actual-home");Set(race,"archiveHome","actual-backup");
            Call(race,"Busy",true,"exporting");Set(race,"browseReady",true);Set(race,"browseHome","frozen-home");Set(race,"browseArchiveHome","frozen-backup");Call(race,"UpdateInteractive");
            var during=(Task)Call(race,"LoadConversation","actual-home","during-export",-1);
            Check(!during.IsCompleted,"Chat can actually start loading during export");
            Check(requestedHomes["during-export"]=="frozen-home","Conversation worker receives frozen path, never live path");
            pending["during-export"].SetResult(Result("during-export-result"));Pump(during);
            Check(((TextBlock)Field(race,"chatTitle")).Text=="during-export-result","Chat rendering completes while operation is still busy");
            Check(((TextBlock)Field(race,"status")).Text=="exporting","Operation progress survives chat completion");
            Set(race,"compareChange",new Change {Id="during-import",Title="import",Project="fixture",Status="newer"});
            var duringImport=(Task)Call(race,"LoadComparison",-1);
            Check(!duringImport.IsCompleted,"Backup preview can actually load during import");
            Check(requestedHomes["during-import"]=="frozen-backup","Comparison worker receives surviving frozen backup path");
            pending["during-import"].SetResult(Result("during-import-result"));Pump(duringImport);
            Check((bool)Field(race,"busy"),"Browsing cannot end an active import/export operation");
            Call(race,"Busy",false,"ready");race.Close();
            Console.WriteLine("UI checks passed: "+assertions);return 0;
        } catch(Exception error) {Console.Error.WriteLine(error);return 1;}
    }
}
