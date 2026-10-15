using System;
using System.Collections;
using System.Collections.Generic;
using System.Collections.Concurrent;
using System.ComponentModel;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Markup;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Threading;
using System.Windows.Data;
using Microsoft.Win32;

namespace CodexZhiru {
    public class Change : INotifyPropertyChanged {
        public string Id { get; set; }
        public string Title { get; set; }
        public string Project { get; set; }
        public string Reason { get; set; }
        public bool Selectable { get; set; }
        public bool Helper { get; set; }
        public string Status { get; set; }
        public Dictionary<string,object> Source { get; set; }
        private bool selected;
        public bool Selected { get { return selected; } set { selected = value; if (PropertyChanged != null) PropertyChanged(this, new PropertyChangedEventArgs("Selected")); } }
        public event PropertyChangedEventHandler PropertyChanged;
    }

    public class MainWindow : Window {
        private static readonly string Root = AppDomain.CurrentDomain.BaseDirectory;
        private readonly JavaScriptSerializer json = new JavaScriptSerializer { MaxJsonLength = 64 * 1024 * 1024, RecursionLimit = 512 };
        private ComboBox profiles;
        private TreeView tree;
        private TextBox search;
        private TextBlock status, stats, chatTitle, chatMeta, pageLabel, diffSummary, homeLabel;
        private StackPanel messages;
        private ScrollViewer messageScroll;
        private ProgressBar progress;
        private TreeView differences;
        private TabControl tabs;
        private CheckBox autoClose, includeTools, showHelpers;
        private StackPanel compareMessages;
        private TextBlock compareTitle, compareMeta, comparePage, selectionSummary;
        private ComboBox compareSide;
        private Button selectAll, selectNone, compareOlder, compareNewer;
        private Change compareChange;
        private int compareStart, compareTotal;
        private bool renderingDifferences;
        private Button exportButton, importButton, applyButton, refreshButton, folderButton, rollbackButton, olderButton, newerButton;
        private List<Dictionary<string, object>> chats = new List<Dictionary<string, object>>();
        private List<Change> changes = new List<Change>();
        private string home, viewHome, viewId, token, archiveHome;
        private int messageStart, messageTotal;
        private bool busy, initializing, closingAfterCleanup;
        private bool browseReady;
        private string browseHome, browseArchiveHome;
        private readonly List<CheckBox> changeChecks = new List<CheckBox>();
        private int reading, conversationRequest, comparisonRequest;
        private readonly bool designMode;
        private readonly Func<Dictionary<string,object>,Task<Dictionary<string,object>>> previewReader;
        private readonly List<string> jobs = new List<string>();
        private readonly string[] arguments;
        private const string DemoMarkdown="### 离线备份示例\n这是一段**虚构的界面演示**，不是用户聊天记录。请选择离线包并核对：\n\n```text\nstatus = READY\nexample_chats = 3\nnetwork = OFFLINE\n```\n\n导入前请检查选择和提示，保留回滚副本。**成功导入所选项不代表所有聊天都已恢复。**\n\n| 示例项目 | 结果 |\n| --- | --- |\n| 新聊天 | 可选导入 |\n| 本机较新的聊天 | 保留本机 |";

        private const string Layout = @"
<Grid xmlns='http://schemas.microsoft.com/winfx/2006/xaml/presentation' xmlns:x='http://schemas.microsoft.com/winfx/2006/xaml' Background='#F4F7F5' UseLayoutRounding='True' TextOptions.TextFormattingMode='Display'>
 <Grid.Resources>
  <Style TargetType='Button'>
   <Setter Property='Padding' Value='15,8'/><Setter Property='Margin' Value='0,0,8,0'/><Setter Property='Background' Value='White'/><Setter Property='BorderBrush' Value='#DCE6DF'/><Setter Property='Foreground' Value='#29473B'/><Setter Property='Cursor' Value='Hand'/><Setter Property='FontSize' Value='12.5'/>
   <Setter Property='Template'><Setter.Value><ControlTemplate TargetType='Button'><Border x:Name='Frame' Background='{TemplateBinding Background}' BorderBrush='{TemplateBinding BorderBrush}' BorderThickness='1' CornerRadius='7' Padding='{TemplateBinding Padding}'><ContentPresenter HorizontalAlignment='Center' VerticalAlignment='Center' RecognizesAccessKey='True'/></Border><ControlTemplate.Triggers>
    <Trigger Property='IsMouseOver' Value='True'><Setter TargetName='Frame' Property='Background' Value='#EDF5F0'/><Setter TargetName='Frame' Property='BorderBrush' Value='#A9C6B6'/></Trigger>
    <MultiTrigger><MultiTrigger.Conditions><Condition Property='IsMouseOver' Value='True'/><Condition Property='Tag' Value='primary'/></MultiTrigger.Conditions><Setter TargetName='Frame' Property='Background' Value='#116A50'/></MultiTrigger>
    <Trigger Property='IsPressed' Value='True'><Setter TargetName='Frame' Property='Opacity' Value='0.8'/></Trigger>
    <Trigger Property='IsKeyboardFocused' Value='True'><Setter TargetName='Frame' Property='BorderBrush' Value='#3D9672'/></Trigger>
    <Trigger Property='IsEnabled' Value='False'><Setter TargetName='Frame' Property='Opacity' Value='0.4'/></Trigger>
   </ControlTemplate.Triggers></ControlTemplate></Setter.Value></Setter>
  </Style>
  <Style TargetType='TextBlock'><Setter Property='Foreground' Value='#233D35'/></Style>
  <Style TargetType='TextBox'><Setter Property='Background' Value='White'/><Setter Property='BorderBrush' Value='#DDE7E1'/><Setter Property='BorderThickness' Value='1'/><Setter Property='Padding' Value='10,8'/><Setter Property='Foreground' Value='#2A4337'/><Setter Property='Template'><Setter.Value><ControlTemplate TargetType='TextBox'><Border x:Name='Input' CornerRadius='7' Background='{TemplateBinding Background}' BorderBrush='{TemplateBinding BorderBrush}' BorderThickness='{TemplateBinding BorderThickness}'><ScrollViewer x:Name='PART_ContentHost'/></Border><ControlTemplate.Triggers><Trigger Property='IsKeyboardFocused' Value='True'><Setter TargetName='Input' Property='BorderBrush' Value='#6AAA87'/></Trigger></ControlTemplate.Triggers></ControlTemplate></Setter.Value></Setter></Style>
  <Style TargetType='TabControl'><Setter Property='Template'><Setter.Value><ControlTemplate TargetType='TabControl'><Grid><Grid.RowDefinitions><RowDefinition Height='Auto'/><RowDefinition Height='*'/></Grid.RowDefinitions><Border Background='#F0F5F2' CornerRadius='8' HorizontalAlignment='Left' Padding='3' Margin='10,8,10,4'><TabPanel IsItemsHost='True'/></Border><ContentPresenter Grid.Row='1' x:Name='PART_SelectedContentHost' ContentSource='SelectedContent' Margin='0,2,0,0'/></Grid></ControlTemplate></Setter.Value></Setter></Style>
  <Style TargetType='TabItem'><Setter Property='Padding' Value='18,8'/><Setter Property='FontWeight' Value='Normal'/><Setter Property='Foreground' Value='#7A8E82'/><Setter Property='Template'><Setter.Value><ControlTemplate TargetType='TabItem'><Border x:Name='Tab' CornerRadius='6' Background='Transparent' Padding='{TemplateBinding Padding}'><ContentPresenter x:Name='TabLabel' ContentSource='Header'/></Border><ControlTemplate.Triggers><Trigger Property='IsSelected' Value='True'><Setter TargetName='Tab' Property='Background' Value='White'/><Setter TargetName='TabLabel' Property='TextElement.Foreground' Value='#17674D'/><Setter TargetName='TabLabel' Property='TextElement.FontWeight' Value='SemiBold'/></Trigger><Trigger Property='IsMouseOver' Value='True'><Setter TargetName='TabLabel' Property='TextElement.Foreground' Value='#1C684E'/></Trigger></ControlTemplate.Triggers></ControlTemplate></Setter.Value></Setter></Style>
  <Style x:Key='BranchArrow' TargetType='ToggleButton'><Setter Property='Focusable' Value='False'/><Setter Property='Width' Value='16'/><Setter Property='Template'><Setter.Value><ControlTemplate TargetType='ToggleButton'><Border Background='Transparent' Width='16' Height='28'><Path x:Name='Chevron' Data='M 1 1 L 5 5 L 1 9' Stroke='#82988A' StrokeThickness='1.5' Width='6' Height='10' RenderTransformOrigin='0.5,0.5'><Path.RenderTransform><RotateTransform Angle='0'/></Path.RenderTransform></Path></Border><ControlTemplate.Triggers><Trigger Property='IsChecked' Value='True'><Setter TargetName='Chevron' Property='RenderTransform'><Setter.Value><RotateTransform Angle='90'/></Setter.Value></Setter></Trigger></ControlTemplate.Triggers></ControlTemplate></Setter.Value></Setter></Style>
  <Style TargetType='TreeView'><Setter Property='Background' Value='Transparent'/><Setter Property='BorderThickness' Value='0'/><Setter Property='Padding' Value='0'/><Setter Property='Foreground' Value='#30483A'/></Style>
  <Style TargetType='TreeViewItem'><Setter Property='HorizontalContentAlignment' Value='Stretch'/><Setter Property='Foreground' Value='#30483A'/><Setter Property='Template'><Setter.Value><ControlTemplate TargetType='TreeViewItem'><Grid><Grid.ColumnDefinitions><ColumnDefinition Width='16'/><ColumnDefinition Width='*'/></Grid.ColumnDefinitions><Grid.RowDefinitions><RowDefinition Height='Auto'/><RowDefinition Height='Auto'/></Grid.RowDefinitions>
   <ToggleButton x:Name='Arrow' Style='{StaticResource BranchArrow}' IsChecked='{Binding IsExpanded,RelativeSource={RelativeSource TemplatedParent},Mode=TwoWay}' VerticalAlignment='Top'/>
   <Border x:Name='Row' Grid.Column='1' CornerRadius='7' Padding='8,7' Margin='0,1,2,1' Background='Transparent' BorderBrush='Transparent' BorderThickness='1'><ContentPresenter x:Name='PART_Header' ContentSource='Header' HorizontalAlignment='{TemplateBinding HorizontalContentAlignment}'/></Border>
   <ItemsPresenter x:Name='Children' Grid.Row='1' Grid.Column='1' Margin='0,1,0,1'/>
  </Grid><ControlTemplate.Triggers>
   <Trigger Property='IsExpanded' Value='False'><Setter TargetName='Children' Property='Visibility' Value='Collapsed'/></Trigger>
   <Trigger Property='HasItems' Value='False'><Setter TargetName='Arrow' Property='Visibility' Value='Hidden'/></Trigger>
   <Trigger SourceName='Row' Property='IsMouseOver' Value='True'><Setter TargetName='Row' Property='Background' Value='#F0F6F2'/></Trigger>
   <Trigger Property='IsSelected' Value='True'><Setter TargetName='Row' Property='Background' Value='#E1F1E7'/><Setter TargetName='Row' Property='BorderBrush' Value='#B7DBC5'/><Setter Property='Foreground' Value='#155E40'/></Trigger>
  </ControlTemplate.Triggers></ControlTemplate></Setter.Value></Setter></Style>
  <Style TargetType='ComboBoxItem'><Setter Property='Padding' Value='10,7'/><Setter Property='Template'><Setter.Value><ControlTemplate TargetType='ComboBoxItem'><Border x:Name='Choice' CornerRadius='5' Padding='{TemplateBinding Padding}' Background='Transparent'><ContentPresenter/></Border><ControlTemplate.Triggers><Trigger Property='IsHighlighted' Value='True'><Setter TargetName='Choice' Property='Background' Value='#EEF6F1'/></Trigger><Trigger Property='IsSelected' Value='True'><Setter TargetName='Choice' Property='Background' Value='#E1F1E7'/></Trigger></ControlTemplate.Triggers></ControlTemplate></Setter.Value></Setter></Style>
  <Style TargetType='ComboBox'><Setter Property='Background' Value='White'/><Setter Property='Padding' Value='8,5'/><Setter Property='Template'><Setter.Value><ControlTemplate TargetType='ComboBox'><Grid>
   <ToggleButton x:Name='DropDown' Focusable='False' ClickMode='Press' IsChecked='{Binding IsDropDownOpen,RelativeSource={RelativeSource TemplatedParent},Mode=TwoWay}'><ToggleButton.Template><ControlTemplate TargetType='ToggleButton'><Border CornerRadius='7' BorderBrush='#DDE7E1' BorderThickness='1' Background='White'><Grid><Path Data='M 1 1 L 5 5 L 9 1' Stroke='#7A9182' StrokeThickness='1.5' Width='10' Height='6' HorizontalAlignment='Right' Margin='0,0,11,0'/></Grid></Border></ControlTemplate></ToggleButton.Template></ToggleButton>
   <ContentPresenter Margin='10,6,30,6' VerticalAlignment='Center' IsHitTestVisible='False' Content='{TemplateBinding SelectionBoxItem}' ContentTemplate='{TemplateBinding SelectionBoxItemTemplate}' ContentTemplateSelector='{TemplateBinding ItemTemplateSelector}' ContentStringFormat='{TemplateBinding SelectionBoxItemStringFormat}'/>
   <Popup x:Name='PART_Popup' IsOpen='{TemplateBinding IsDropDownOpen}' Placement='Bottom' AllowsTransparency='True' Focusable='False'><Border Background='White' BorderBrush='#DDE7E1' BorderThickness='1' CornerRadius='8' Padding='4' MinWidth='{Binding ActualWidth,RelativeSource={RelativeSource TemplatedParent}}'><ScrollViewer MaxHeight='260'><ItemsPresenter/></ScrollViewer></Border></Popup>
  </Grid></ControlTemplate></Setter.Value></Setter></Style>
 </Grid.Resources>
 <Grid.RowDefinitions><RowDefinition Height='Auto'/><RowDefinition Height='Auto'/><RowDefinition Height='*'/><RowDefinition Height='Auto'/></Grid.RowDefinitions>
 <Border Background='White' Padding='22,14' BorderBrush='#E1EAE4' BorderThickness='0,0,0,1'>
  <Grid><Grid.ColumnDefinitions><ColumnDefinition Width='Auto'/><ColumnDefinition Width='*'/><ColumnDefinition Width='Auto'/></Grid.ColumnDefinitions>
   <Border Background='#17674D' Width='42' Height='42' CornerRadius='12' Margin='0,0,12,0'><TextBlock Text='↕' Foreground='White' FontSize='29' HorizontalAlignment='Center' VerticalAlignment='Center'/></Border>
   <StackPanel Grid.Column='1'><TextBlock Text='codex智入' FontSize='22' FontWeight='SemiBold' Foreground='#184B37'/><TextBlock Text='把对话带走，把新内容留下。' Margin='0,3,0,0' FontSize='11.5' Foreground='#82968A'/></StackPanel>
   <Border Grid.Column='2' Background='#EDF6F0' CornerRadius='12' Padding='12,6' VerticalAlignment='Center'><TextBlock Text='●  纯本地运行   /   1.3' Foreground='#52836A' FontSize='11'/></Border>
  </Grid>
 </Border>
 <StackPanel Grid.Row='1' Margin='22,14,22,12'>
  <WrapPanel><Button x:Name='Export' Content='↑  一键导出到桌面' Tag='primary' Background='#19785E' Foreground='White' BorderBrush='#19785E'/><Button x:Name='Import' Content='↓  一键导入 / 核对'/><Button x:Name='Refresh' Content='刷新聊天'/><Button x:Name='Rollback' Content='恢复到导入前'/><CheckBox x:Name='AutoClose' Content='操作前请求正常退出 Codex' IsChecked='True' VerticalAlignment='Center' Margin='12,0,0,0'/></WrapPanel>
  <Grid Margin='0,12,0,0'><Grid.ColumnDefinitions><ColumnDefinition Width='Auto'/><ColumnDefinition Width='*'/><ColumnDefinition Width='Auto'/></Grid.ColumnDefinitions>
   <TextBlock Text='数据目录' VerticalAlignment='Center' Margin='0,0,12,0'/><ComboBox x:Name='Profiles' Grid.Column='1' MinHeight='30' Padding='6' DisplayMemberPath='Label'/><Button x:Name='Folder' Grid.Column='2' Content='选择其他目录' Padding='12,6' Margin='10,0,0,0'/>
  </Grid>
  <TextBlock x:Name='HomeLabel' FontSize='11' Foreground='#667D74' Margin='62,6,0,0' TextTrimming='CharacterEllipsis'/>
 </StackPanel>
 <Grid Grid.Row='2' Margin='22,0,22,16'>
  <Grid.ColumnDefinitions><ColumnDefinition Width='295'/><ColumnDefinition Width='14'/><ColumnDefinition Width='*'/></Grid.ColumnDefinitions>
  <Border Background='White' BorderBrush='#DFE7E3' BorderThickness='1' CornerRadius='10' Padding='13'>
   <Grid><Grid.RowDefinitions><RowDefinition Height='Auto'/><RowDefinition Height='Auto'/><RowDefinition Height='Auto'/><RowDefinition Height='Auto'/><RowDefinition Height='*'/></Grid.RowDefinitions>
    <TextBlock Text='本机聊天' FontWeight='SemiBold' FontSize='16'/><TextBlock x:Name='Stats' Grid.Row='1' Text='正在寻找聊天数据…' Foreground='#81958A' FontSize='11' Margin='0,7,0,12' TextWrapping='Wrap' LineHeight='18'/>
    <Grid Grid.Row='2' Margin='0,0,0,12'><TextBox x:Name='Search' Padding='10,8' ToolTip='按聊天名称或项目名称搜索'/><TextBlock Text='搜索聊天或项目…' Foreground='#A0B1A6' Margin='12,0,0,0' VerticalAlignment='Center' IsHitTestVisible='False'><TextBlock.Style><Style TargetType='TextBlock'><Setter Property='Visibility' Value='Collapsed'/><Style.Triggers><DataTrigger Binding='{Binding Text,ElementName=Search}' Value=''><Setter Property='Visibility' Value='Visible'/></DataTrigger></Style.Triggers></Style></TextBlock.Style></TextBlock></Grid>
    <CheckBox x:Name='ShowHelpers' Grid.Row='3' Content='显示内部助手 / 安全审查' Margin='0,0,0,10' FontSize='11'/>
    <TreeView x:Name='Chats' Grid.Row='4' BorderThickness='0' Background='Transparent' ScrollViewer.HorizontalScrollBarVisibility='Disabled'/>
   </Grid>
  </Border>
  <Border Grid.Column='2' Background='White' BorderBrush='#DFE7E3' BorderThickness='1' CornerRadius='10' Padding='6'>
   <TabControl x:Name='Tabs' BorderThickness='0' Background='White'>
    <TabItem Header='对话预览'><Grid Margin='12'>
     <Grid.RowDefinitions><RowDefinition Height='Auto'/><RowDefinition Height='Auto'/><RowDefinition Height='*'/><RowDefinition Height='Auto'/></Grid.RowDefinitions>
     <TextBlock x:Name='ChatTitle' Text='选择一条聊天，继续读它的故事' FontSize='19' FontWeight='SemiBold' TextWrapping='Wrap'/>
     <Grid Grid.Row='1' Margin='0,7,0,12'><Grid.ColumnDefinitions><ColumnDefinition Width='*'/><ColumnDefinition Width='Auto'/></Grid.ColumnDefinitions><TextBlock x:Name='ChatMeta' Text='项目、归档和分支聊天会一起显示在左侧。' Foreground='#70867C' FontSize='12' TextWrapping='Wrap'/><CheckBox x:Name='Tools' Grid.Column='1' Content='显示工具记录' Margin='8,0,0,0'/></Grid>
     <ScrollViewer x:Name='MessageScroll' Grid.Row='2' VerticalScrollBarVisibility='Auto' HorizontalScrollBarVisibility='Disabled' Background='#F8FAF9'><StackPanel x:Name='Messages' Margin='14'/></ScrollViewer>
     <DockPanel Grid.Row='3' Margin='0,12,0,0'><Button x:Name='Older' Content='← 更早的对话' Padding='12,6'/><Button x:Name='Newer' Content='后面的对话 →' Padding='12,6'/><TextBlock x:Name='PageLabel' VerticalAlignment='Center' Foreground='#70867C' HorizontalAlignment='Right'/></DockPanel>
    </Grid></TabItem>
    <TabItem Header='导入核对'><Grid Margin='12'>
     <Grid.RowDefinitions><RowDefinition Height='Auto'/><RowDefinition Height='Auto'/><RowDefinition Height='*'/><RowDefinition Height='Auto'/></Grid.RowDefinitions>
     <TextBlock Text='只带入需要更新的对话' FontSize='19' FontWeight='SemiBold'/>
     <TextBlock x:Name='DiffSummary' Grid.Row='1' Text='点击“一键导入”，选择桌面上的 .czr 备份包。内容相同或本机更新的聊天不会出现在待更新列表。' TextWrapping='Wrap' Margin='0,9,0,15' Foreground='#70867C'/>
     <Grid Grid.Row='2'><Grid.ColumnDefinitions><ColumnDefinition Width='260'/><ColumnDefinition Width='12'/><ColumnDefinition Width='*'/></Grid.ColumnDefinitions>
      <Border BorderBrush='#DEE7E2' BorderThickness='1' CornerRadius='6' Padding='6'><TreeView x:Name='Differences' BorderThickness='0' ScrollViewer.HorizontalScrollBarVisibility='Disabled'/></Border>
      <Grid Grid.Column='2'><Grid.RowDefinitions><RowDefinition Height='Auto'/><RowDefinition Height='Auto'/><RowDefinition Height='*'/><RowDefinition Height='Auto'/></Grid.RowDefinitions>
       <TextBlock x:Name='CompareTitle' Text='点选一条聊天查看对话' FontSize='16' FontWeight='SemiBold' TextWrapping='Wrap'/>
       <StackPanel Grid.Row='1' Margin='0,6,0,10'><TextBlock x:Name='CompareMeta' Text='左边勾选要导入的聊天，右边直接阅读内容。' Foreground='#70867C' FontSize='11' TextWrapping='Wrap'/><ComboBox x:Name='CompareSide' Margin='0,8,0,0' SelectedIndex='0' Padding='6'><ComboBoxItem Content='备份中的对话'/><ComboBoxItem Content='这台电脑的对话'/></ComboBox></StackPanel>
       <ScrollViewer Grid.Row='2' VerticalScrollBarVisibility='Auto' HorizontalScrollBarVisibility='Disabled' Background='#F8FAF9'><StackPanel x:Name='CompareMessages' Margin='8'/></ScrollViewer>
       <WrapPanel Grid.Row='3' Margin='0,9,0,0'><Button x:Name='CompareOlder' Content='更早' Padding='8,4'/><Button x:Name='CompareNewer' Content='后面' Padding='8,4'/><TextBlock x:Name='ComparePage' VerticalAlignment='Center' FontSize='11'/></WrapPanel>
      </Grid>
     </Grid>
     <StackPanel Grid.Row='3' Margin='0,12,0,0'><TextBlock x:Name='SelectionSummary' Text='尚未核对备份包。' Foreground='#70867C' FontSize='11' TextWrapping='Wrap' Margin='0,0,0,10'/><WrapPanel><Button x:Name='SelectAll' Content='勾选全部可导入项'/><Button x:Name='SelectNone' Content='全部取消'/><Button x:Name='Apply' Content='确认导入所选聊天' Tag='primary' Background='#19785E' Foreground='White' BorderBrush='#19785E' IsEnabled='False'/></WrapPanel></StackPanel>
    </Grid></TabItem>
   </TabControl>
  </Border>
 </Grid>
 <Border Grid.Row='3' Background='#E7EFEB' Padding='22,10'><Grid><Grid.ColumnDefinitions><ColumnDefinition Width='*'/><ColumnDefinition Width='160'/></Grid.ColumnDefinitions><TextBlock x:Name='Status' Text='离线就绪 · 浏览聊天不会修改原始记录' FontSize='12' TextTrimming='CharacterEllipsis'/><ProgressBar x:Name='Progress' Grid.Column='1' Height='5' Visibility='Collapsed' IsIndeterminate='True'/></Grid></Border>
</Grid>";

        public MainWindow(string[] args) : this(args,null) { }
        public MainWindow(string[] args,Func<Dictionary<string,object>,Task<Dictionary<string,object>>> testPreview) {
            arguments = args;
            designMode=args.Length>0&&(args[0].StartsWith("--smoke")||args[0]=="--ui-test");
            if(testPreview!=null&&!designMode) throw new InvalidOperationException("Preview fixture is available only in UI test mode.");
            previewReader=testPreview;
            Title = "codex智入 1.3 · 离线聊天迁移"; Width = Math.Min(1360,SystemParameters.WorkArea.Width-24); Height = Math.Min(850,SystemParameters.WorkArea.Height-24); MinWidth = Math.Min(1020,Width); MinHeight = Math.Min(650,Height);
            FontFamily = RichMessage.BodyFont; FontSize = 13;
            WindowStartupLocation = WindowStartupLocation.CenterScreen;
            if (designMode) { WindowStartupLocation=WindowStartupLocation.Manual; Left=-3000; Top=-3000; ShowInTaskbar=false; }
            Grid layout = (Grid)XamlReader.Parse(Layout); Content = layout;
            profiles = (ComboBox)layout.FindName("Profiles"); tree = (TreeView)layout.FindName("Chats"); search = (TextBox)layout.FindName("Search");
            status = (TextBlock)layout.FindName("Status"); stats = (TextBlock)layout.FindName("Stats"); homeLabel = (TextBlock)layout.FindName("HomeLabel");
            chatTitle = (TextBlock)layout.FindName("ChatTitle"); chatMeta = (TextBlock)layout.FindName("ChatMeta"); pageLabel = (TextBlock)layout.FindName("PageLabel"); diffSummary = (TextBlock)layout.FindName("DiffSummary");
            messages = (StackPanel)layout.FindName("Messages"); messageScroll = (ScrollViewer)layout.FindName("MessageScroll");
            progress = (ProgressBar)layout.FindName("Progress"); differences = (TreeView)layout.FindName("Differences"); tabs = (TabControl)layout.FindName("Tabs");
            showHelpers=(CheckBox)layout.FindName("ShowHelpers"); compareMessages=(StackPanel)layout.FindName("CompareMessages");
            compareTitle=(TextBlock)layout.FindName("CompareTitle"); compareMeta=(TextBlock)layout.FindName("CompareMeta"); comparePage=(TextBlock)layout.FindName("ComparePage");
            selectionSummary=(TextBlock)layout.FindName("SelectionSummary"); compareSide=(ComboBox)layout.FindName("CompareSide");
            selectAll=(Button)layout.FindName("SelectAll"); selectNone=(Button)layout.FindName("SelectNone");
            compareOlder=(Button)layout.FindName("CompareOlder"); compareNewer=(Button)layout.FindName("CompareNewer");
            selectNone.ToolTip="取消所有导入勾选，不删除聊天；可以重新勾选。";
            autoClose = (CheckBox)layout.FindName("AutoClose"); includeTools = (CheckBox)layout.FindName("Tools");
            exportButton = (Button)layout.FindName("Export"); importButton = (Button)layout.FindName("Import"); applyButton = (Button)layout.FindName("Apply");
            refreshButton = (Button)layout.FindName("Refresh"); folderButton = (Button)layout.FindName("Folder"); rollbackButton = (Button)layout.FindName("Rollback");
            olderButton = (Button)layout.FindName("Older"); newerButton = (Button)layout.FindName("Newer");
            Loaded += async delegate { if (args.Length>0&&args[0]=="--ui-test") return; if (designMode) { Smoke(args); } else { await Initialize(); } };
            Closing += async delegate(object sender, CancelEventArgs e) {
                if(designMode) return;
                if (busy || reading>0) { MessageBox.Show(this, "操作正在进行，请等待完成再关闭窗口。", "请稍候"); e.Cancel = true; }
                else if (!closingAfterCleanup && File.Exists(Path.Combine(Root,".runtime","python.exe"))) {
                    e.Cancel=true; Busy(true,"清理本次核对产生的临时副本…");
                    foreach (string job in jobs.ToArray()) { try { var req=Request("cleanup"); req["token"]=job; await Worker(req); jobs.Remove(job); } catch { } }
                    try { await Worker(Request("cleanup_views")); } catch { }
                    closingAfterCleanup=true; Busy(false,"已结束"); Close();
                }
            };
            profiles.SelectionChanged += async delegate { if (!initializing && !busy && profiles.SelectedItem != null) { home = ((Profile)profiles.SelectedItem).Path; homeLabel.Text = home; Busy(true,"切换数据目录…"); await DiscardPreview(); Busy(false,"目录已切换"); await Refresh(); } };
            search.TextChanged += delegate { RenderTree(); };
            tree.SelectedItemChanged += async delegate { TreeViewItem item = tree.SelectedItem as TreeViewItem; if (!designMode && item != null && item.Tag is string) await LoadConversation(home, (string)item.Tag, -1); };
            refreshButton.Click += async delegate { await Refresh(); };
            folderButton.Click += async delegate { await ChooseFolder(); };
            exportButton.Click += async delegate { await Export(); };
            importButton.Click += async delegate { await Preview(); };
            applyButton.Click += async delegate { await Apply(); };
            rollbackButton.Click += async delegate { await Rollback(); };
            olderButton.Click += async delegate { if (viewId != null) await LoadConversation(viewHome, viewId, Math.Max(0, messageStart - 60)); };
            newerButton.Click += async delegate { if (viewId != null) await LoadConversation(viewHome, viewId, messageStart + 60); };
            includeTools.Click += async delegate { if (viewId != null) await LoadConversation(viewHome, viewId, -1); };
            selectAll.Click += delegate { if (!busy) { foreach (Change c in changes) c.Selected=c.Selectable; UpdateSelection(); status.Text="已勾选全部可导入项。"; } };
            selectNone.Click += delegate { if (!busy) { foreach (Change c in changes) c.Selected=false; UpdateSelection(); status.Text="已全部取消勾选，当前选择 0 条；聊天和备份包均未修改。"; } };
            differences.SelectedItemChanged += async delegate { var node=differences.SelectedItem as TreeViewItem; var change=node==null?null:node.Tag as Change; if(!designMode && !renderingDifferences && change!=null) { compareChange=change; await LoadComparison(-1); } };
            compareSide.SelectionChanged += async delegate { if(compareChange!=null) await LoadComparison(-1); };
            compareOlder.Click += async delegate { await LoadComparison(Math.Max(0,compareStart-60)); };
            compareNewer.Click += async delegate { await LoadComparison(compareStart+60); };
            showHelpers.Click += delegate { RenderTree(); };
        }

        private class Profile { public string Path { get; set; } public string Label { get; set; } }
        private static string S(Dictionary<string, object> d, string k) { return d.ContainsKey(k) && d[k] != null ? Convert.ToString(d[k], CultureInfo.InvariantCulture) : ""; }
        private static int N(Dictionary<string, object> d, string k) { return d.ContainsKey(k) ? Convert.ToInt32(d[k]) : 0; }
        private static bool B(Dictionary<string, object> d, string k) { return d.ContainsKey(k) && Convert.ToBoolean(d[k]); }
        private static IEnumerable<Dictionary<string, object>> Rows(Dictionary<string, object> d, string k) { return ((IEnumerable)d[k]).Cast<object>().Select(x => (Dictionary<string, object>)x); }
        private static Dictionary<string, object> Request(string action) { return new Dictionary<string, object> { { "action", action } }; }
        private void Busy(bool value, string text) {
            busy = value; status.Text = text; progress.Visibility = value || reading>0 ? Visibility.Visible : Visibility.Collapsed;
            if(!value) browseReady=false;
            if(value) progress.IsIndeterminate=true;
            UpdateInteractive();
        }
        private void UpdateInteractive() {
            bool ready=!busy&&reading==0;
            bool readable=!busy||browseReady;
            exportButton.IsEnabled=importButton.IsEnabled=refreshButton.IsEnabled=folderButton.IsEnabled=rollbackButton.IsEnabled=profiles.IsEnabled=ready;
            tree.IsEnabled=differences.IsEnabled=readable;
            compareSide.IsEnabled=includeTools.IsEnabled=readable;
            foreach(var check in changeChecks) check.IsEnabled=!busy && ((Change)check.Tag).Selectable;
            UpdateSelection(); compareOlder.IsEnabled=readable&&compareStart>0;compareNewer.IsEnabled=readable&&compareStart+60<compareTotal;
            olderButton.IsEnabled=readable&&messageStart>0;newerButton.IsEnabled=readable&&messageStart+60<messageTotal;
        }
        private void BeginReading(string text) {
            reading++; if(!busy) {status.Text=text;progress.Visibility=Visibility.Visible;progress.IsIndeterminate=true;} UpdateInteractive();
        }
        private void EndReading(string text) {
            reading=Math.Max(0,reading-1);
            if(!busy&&reading==0) progress.Visibility=Visibility.Collapsed;
            if(text!=null&&!busy) status.Text=text;
            UpdateInteractive();
        }
        private void Error(Exception ex) { Busy(false, "操作未完成：" + ex.Message); MessageBox.Show(this, ex.Message, "codex智入 · 提示", MessageBoxButton.OK, MessageBoxImage.Warning); }
        private string ReadHome(string source) {
            if(!busy) return source;
            if(!browseReady) return null;
            if(source==home) return browseHome;
            if(source==archiveHome) return browseArchiveHome;
            return null;
        }
        private async Task PrepareBrowsing() {
            var req=Request("prepare_readers");req["home"]=home;req["auto_close"]=autoClose.IsChecked==true;
            if(!String.IsNullOrEmpty(archiveHome)) req["archive_home"]=archiveHome;
            var result=await Worker(req);jobs.Add(S(result,"token"));
            browseHome=S(result,"home");browseArchiveHome=S(result,"archive_home");browseReady=true;UpdateInteractive();
        }
        private async Task<Dictionary<string, object>> Worker(Dictionary<string, object> request) {
            if(designMode&&previewReader!=null&&S(request,"action")=="conversation") return await previewReader(request);
            string input = json.Serialize(request);
            return await Task.Run(delegate {
                string python = Path.Combine(Root, ".runtime", "python.exe");
                var start = new ProcessStartInfo(python, "-I -X utf8 \"" + Path.Combine(Root, "src", "offline.py") + "\"") {
                    UseShellExecute = false, CreateNoWindow = true, RedirectStandardInput = true, RedirectStandardOutput = true, RedirectStandardError = true,
                    StandardOutputEncoding = Encoding.UTF8, StandardErrorEncoding = Encoding.UTF8, WorkingDirectory = Root
                };
                using (Process process = Process.Start(start)) {
                    var errorTask = process.StandardError.ReadToEndAsync();
                    byte[] bytes = new UTF8Encoding(false).GetBytes(input);
                    process.StandardInput.BaseStream.Write(bytes, 0, bytes.Length); process.StandardInput.Close();
                    Dictionary<string, object> result = null; string failure = null;
                    string line;
                    while ((line = process.StandardOutput.ReadLine()) != null) {
                        if (String.IsNullOrWhiteSpace(line)) continue;
                        Dictionary<string, object> item = new JavaScriptSerializer { MaxJsonLength = 64 * 1024 * 1024, RecursionLimit = 512 }.Deserialize<Dictionary<string, object>>(line);
                        string kind = S(item, "type");
                        if (kind == "progress") { string message = S(item, "message"); object pct=item.ContainsKey("percent")?item["percent"]:null; Dispatcher.BeginInvoke(new Action(delegate { status.Text = message; progress.IsIndeterminate=pct==null; if(pct!=null) progress.Value=Convert.ToDouble(pct); })); }
                        else if (kind == "result") result = (Dictionary<string, object>)item["data"];
                        else if (kind == "error") failure = S(item, "message");
                    }
                    process.WaitForExit(); string detail = errorTask.Result;
                    if (failure != null) throw new InvalidOperationException(failure);
                    if (process.ExitCode != 0 || result == null) throw new InvalidOperationException("离线工作进程未完成。请确认工具文件齐全，并检查剩余磁盘空间。" + (String.IsNullOrWhiteSpace(detail) ? "" : "\n" + detail));
                    return result;
                }
            });
        }
        private async Task Initialize() {
            Busy(true, "准备离线运行环境…");
            try {
                await Task.Run(new Action(Program.PrepareRuntime));
                Dictionary<string, object> data = await Worker(Request("discover"));
                initializing = true;
                foreach (var p in Rows(data, "profiles")) profiles.Items.Add(new Profile { Path = S(p, "path"), Label = S(p, "label") + (B(p, "exists") ? "" : "（尚未初始化，可导入）") });
                profiles.SelectedIndex = 0; home = ((Profile)profiles.SelectedItem).Path; homeLabel.Text = home; initializing = false;
                Busy(false, "离线运行环境就绪"); await Refresh();
            } catch (Exception ex) { initializing = false; Error(ex); }
        }
        private async Task Refresh() {
            if (busy || String.IsNullOrEmpty(home)) return;
            Busy(true, "只读扫描本机聊天…");
            conversationRequest++; comparisonRequest++;
            try {
                if (!File.Exists(Path.Combine(home, "state_5.sqlite"))) { chats.Clear(); RenderTree(); stats.Text = "尚无聊天数据。可以先启动 Codex 登录，或直接导入离线包。"; Busy(false, "未初始化的数据目录 · 可导入备份"); return; }
                var req = Request("list"); req["home"] = home;
                var result = await Worker(req); chats = Rows(result, "chats").ToList();
                stats.Text = String.Format("{0} 条主聊天 · {1} 个项目 · {2} 条内部助手\n按 Codex 的置顶、项目和聊天顺序显示", chats.Count-N(result,"helpers"), N(result, "projects"), N(result, "helpers"));
                RenderTree(); Busy(false, "读取完成 · 原始聊天未修改");
            } catch (Exception ex) { Error(ex); }
        }
        private void RenderTree() {
            if (tree == null) return;
            tree.Items.Clear(); string query = search.Text.Trim();
            var visible = chats.Where(c => (showHelpers.IsChecked==true || !B(c,"helper")) && (query.Length == 0 || (S(c,"title") + " " + S(c,"project")).IndexOf(query, StringComparison.CurrentCultureIgnoreCase) >= 0)).ToList();
            foreach (var group in visible.GroupBy(c => S(c,"group_key").Length>0?S(c,"group_key"):S(c, "project_id") + "\n" + S(c, "project"))) {
                string heading=S(group.First(),"group_name"); if(heading.Length==0) heading=S(group.First(),"project");
                TreeViewItem project = new TreeViewItem { Header = new TextBlock {Text=heading+"  ·  "+group.Count(),FontSize=12,FontWeight=FontWeights.SemiBold,TextTrimming=TextTrimming.CharacterEllipsis}, IsExpanded = query.Length>0 || S(group.First(),"group_key")!="archived", FontWeight = FontWeights.SemiBold, Margin = new Thickness(0, 4, 0, 6) };
                var nodes = new Dictionary<string, TreeViewItem>();
                foreach (var row in group) {
                    TextBlock label = new TextBlock { Text = (B(row,"archived") ? "▧ " : "") + S(row,"title"), MaxWidth = 230, TextTrimming = TextTrimming.CharacterEllipsis, FontWeight = FontWeights.Normal };
                    nodes[S(row,"id")] = new TreeViewItem { Header = label, Tag = S(row,"id"), ToolTip = S(row,"title") + (B(row,"archived") ? "\n已归档" : ""), IsExpanded = false };
                }
                foreach (var row in group) {
                    string id = S(row,"id"), parent = S(row,"parent_id"); bool cyclic = false; var seen = new HashSet<string> { id }; string next = parent;
                    while (nodes.ContainsKey(next)) { if (!seen.Add(next)) { cyclic = true; break; } var ancestor = group.FirstOrDefault(c => S(c,"id") == next); next = ancestor == null ? "" : S(ancestor,"parent_id"); }
                    if (!cyclic && nodes.ContainsKey(parent)) nodes[parent].Items.Add(nodes[id]); else project.Items.Add(nodes[id]);
                }
                tree.Items.Add(project);
            }
        }
        private async Task LoadConversation(string source, string id, int start) {
            string readHome=ReadHome(source);if(String.IsNullOrEmpty(readHome)) return;
            int request=++conversationRequest;string outcome=null;
            BeginReading("正在读取所选对话…");
            try {
                var req = Request("conversation"); req["home"] = readHome; req["id"] = id; req["start"] = start; req["tools"] = includeTools.IsChecked == true;
                var result = await Worker(req); var chat = (Dictionary<string,object>)result["chat"];
                if(request!=conversationRequest) return;
                viewHome = source; viewId = id; messageStart = N(result,"start"); messageTotal = N(result,"total");
                chatTitle.Text = S(chat,"title"); chatMeta.Text = (source == home ? "本机 · " : "备份预览 · ") + S(chat,"project") + (B(chat,"archived") ? " · 已归档" : "") + (S(chat,"parent_id").Length > 0 ? " · 分支聊天" : "");
                if(busy) chatMeta.Text += " · 操作前的只读副本";
                ShowMessages(Rows(result,"messages")); tabs.SelectedIndex = 0;
                pageLabel.Text = messageTotal == 0 ? "暂无可展示的消息" : String.Format("{0}–{1} / {2} 条", messageStart + 1, Math.Min(messageStart + 60,messageTotal), messageTotal);
                outcome="对话已就绪 · 支持排版阅读和复制原文"; messageScroll.ScrollToEnd();
            } catch (Exception ex) { if(request==conversationRequest) {outcome="读取未完成："+ex.Message;messages.Children.Clear();messages.Children.Add(new TextBlock {Text=ex.Message,TextWrapping=TextWrapping.Wrap,Margin=new Thickness(16)});} }
            finally {EndReading(outcome);}
        }
        private void ShowMessages(IEnumerable<Dictionary<string,object>> rows) { ShowMessages(rows,messages); }
        private void ShowMessages(IEnumerable<Dictionary<string,object>> rows, StackPanel destination) {
            destination.Children.Clear();
            foreach (var row in rows) {
                bool user = S(row,"role") == "user"; bool tool = S(row,"role") == "tool";
                bool compact=destination==compareMessages;
                Border bubble = new Border { Background = (Brush)new BrushConverter().ConvertFromString(user ? "#ECF6EF" : "#FFFFFF"), BorderBrush = (Brush)new BrushConverter().ConvertFromString(user?"#DCEDE2":"#E7EDE9"), BorderThickness = new Thickness(1), CornerRadius = new CornerRadius(12), Padding = new Thickness(compact?13:18), Margin = new Thickness(user?(compact?12:46):0,0,0,16),MaxWidth=980,HorizontalAlignment=HorizontalAlignment.Stretch };
                StackPanel stack = new StackPanel();
                var header=new DockPanel {LastChildFill=true,Margin=new Thickness(0,0,0,12)};
                string original=S(row,"text");
                Action<string> copy=delegate(string value) {try {Clipboard.SetText(value);status.Text="已复制到剪贴板";} catch {status.Text="剪贴板暂时被占用，请再试一次。";}};
                var copyButton=new Button {Content="复制",ToolTip="复制完整原文",FontSize=11,Padding=new Thickness(7,3,7,3),Margin=new Thickness(6,0,0,0),Background=Brushes.Transparent,BorderBrush=Brushes.Transparent};
                copyButton.Click+=delegate {copy(original);};DockPanel.SetDock(copyButton,Dock.Right);header.Children.Add(copyButton);
                string stamp=S(row,"time");DateTimeOffset parsed;
                if(DateTimeOffset.TryParse(stamp,CultureInfo.InvariantCulture,DateTimeStyles.AllowWhiteSpaces,out parsed)) stamp=parsed.ToLocalTime().ToString("yyyy-MM-dd HH:mm");
                var identity=new StackPanel {Orientation=Orientation.Horizontal,VerticalAlignment=VerticalAlignment.Center};
                var avatar=new Border {Width=25,Height=25,CornerRadius=new CornerRadius(8),Background=(Brush)new BrushConverter().ConvertFromString(user?"#D8EDDF":"#E9F1ED"),Margin=new Thickness(0,0,8,0)};
                avatar.Child=new TextBlock {Text=user?"你":tool?"↳":"C",HorizontalAlignment=HorizontalAlignment.Center,VerticalAlignment=VerticalAlignment.Center,FontSize=12,FontWeight=FontWeights.SemiBold,Foreground=(Brush)new BrushConverter().ConvertFromString("#498064")};identity.Children.Add(avatar);
                identity.Children.Add(new TextBlock {Text=user?"你":tool?"工具记录":"Codex",FontWeight=FontWeights.SemiBold,FontSize=12,VerticalAlignment=VerticalAlignment.Center});
                identity.Children.Add(new TextBlock {Text=stamp,FontSize=10.5,Foreground=(Brush)new BrushConverter().ConvertFromString("#91A297"),Margin=new Thickness(10,0,0,0),VerticalAlignment=VerticalAlignment.Center});
                header.Children.Add(identity);stack.Children.Add(header);
                stack.Children.Add(RichMessage.View(original,tool,copy));
                bubble.Child = stack; destination.Children.Add(bubble);
            }
        }
        private void UpdateSelection() {
            if(selectAll==null) return;
            int eligible=changes.Count(c=>c.Selectable), selected=changes.Count(c=>c.Selectable && c.Selected);
            selectAll.IsEnabled=!busy && eligible>0 && selected<eligible;
            selectNone.IsEnabled=!busy && selected>0;
            applyButton.IsEnabled=!busy && reading==0 && token!=null && selected>0;
            selectionSummary.Text=token==null?"请选择一个备份包进行核对。":eligible==0?"没有可导入的变更。相同或本机更新的聊天已经跳过；冲突项可查看内容。":
                String.Format("已选 {0} / {1} 条可导入记录（含 {2} 条内部助手）。同名附件分别保留，导入失败后可以调整勾选重试。",selected,eligible,changes.Count(c=>c.Selectable && c.Selected && c.Helper));
        }
        private void RenderDifferences() {
            renderingDifferences=true; differences.Items.Clear(); changeChecks.Clear();
            foreach(var group in changes.GroupBy(c=>c.Project)) {
                var root=new TreeViewItem {Header=group.Key+" · "+group.Count(),IsExpanded=true,FontWeight=FontWeights.SemiBold,Margin=new Thickness(0,5,0,8)};
                var helpers=new TreeViewItem {Header="内部助手 / 安全审查 · "+group.Count(c=>c.Helper),IsExpanded=false,FontWeight=FontWeights.Normal};
                foreach(var change in group.OrderBy(c=>c.Helper)) {
                    var stack=new StackPanel {Width=192,Margin=new Thickness(0,4,0,6)};
                    var header=new DockPanel();
                    var check=new CheckBox {IsEnabled=!busy&&change.Selectable,Tag=change,Margin=new Thickness(0,2,7,0),VerticalAlignment=VerticalAlignment.Top};
                    changeChecks.Add(check);
                    check.SetBinding(CheckBox.IsCheckedProperty,new Binding("Selected") {Source=change,Mode=BindingMode.TwoWay,UpdateSourceTrigger=UpdateSourceTrigger.PropertyChanged});
                    header.Children.Add(check); header.Children.Add(new TextBlock {Text=change.Title,TextWrapping=TextWrapping.Wrap,FontWeight=FontWeights.Normal});
                    stack.Children.Add(header); stack.Children.Add(new TextBlock {Text=change.Reason,FontSize=11,Foreground=Brushes.Gray,TextWrapping=TextWrapping.Wrap,Margin=new Thickness(21,4,0,0)});
                    var node=new TreeViewItem {Header=stack,Tag=change,ToolTip=change.Title+"\n"+change.Reason};
                    if(change.Helper) helpers.Items.Add(node); else root.Items.Add(node);
                }
                if(helpers.Items.Count>0) root.Items.Add(helpers); differences.Items.Add(root);
            }
            renderingDifferences=false; UpdateSelection();
        }
        private async Task LoadComparison(int start) {
            if(compareChange==null || archiveHome==null) return;
            bool local=compareSide.SelectedIndex==1;
            string readHome=ReadHome(local?home:archiveHome);if(String.IsNullOrEmpty(readHome)) return;
            int request=++comparisonRequest;Change chosen=compareChange;string outcome=null;
            BeginReading("正在读取核对对话…");
            try {
                compareTitle.Text=chosen.Title;
                compareMeta.Text=chosen.Project+" · "+chosen.Reason+(chosen.Helper?"\n这是 Codex 自动生成的子助手/安全检查，相关主聊天："+S(chosen.Source??new Dictionary<string,object>(),"parent_title"):"");
                if(busy) compareMeta.Text += " · 操作前的只读副本";
                compareMessages.Children.Clear();
                if(local && chosen.Status=="new") {
                    compareMessages.Children.Add(new TextBlock {Text="这台电脑还没有这条聊天。导入后会新增。",TextWrapping=TextWrapping.Wrap,Margin=new Thickness(12)}); compareStart=compareTotal=0; comparePage.Text="";
                } else {
                    var req=Request("conversation"); req["home"]=readHome; req["id"]=chosen.Id; req["start"]=start;
                    var result=await Worker(req);if(request!=comparisonRequest) return;ShowMessages(Rows(result,"messages"),compareMessages);
                    compareStart=N(result,"start"); compareTotal=N(result,"total");
                    comparePage.Text=compareTotal==0?"暂无消息":String.Format("{0}–{1} / {2}",compareStart+1,Math.Min(compareStart+60,compareTotal),compareTotal);
                }
                outcome="核对预览 · 左边勾选，右边阅读对话";
            } catch(Exception ex) { if(request==comparisonRequest) {compareMessages.Children.Clear();compareMessages.Children.Add(new TextBlock {Text=ex.Message,TextWrapping=TextWrapping.Wrap,Margin=new Thickness(12)});outcome="对话预览未完成，导入选择仍保留";} }
            finally {EndReading(outcome);}
        }
        private async Task ChooseFolder() {
            using (var dialog = new System.Windows.Forms.FolderBrowserDialog()) {
                dialog.Description = "选择 Codex 数据目录（通常是用户目录中的 .codex），或新建一个空数据目录。";
                dialog.SelectedPath = home;
                if (dialog.ShowDialog() != System.Windows.Forms.DialogResult.OK) return;
                home = dialog.SelectedPath; initializing = true; var p = new Profile { Path = home, Label = "手动选择的数据目录" }; profiles.Items.Add(p); profiles.SelectedItem = p; initializing = false; homeLabel.Text = home; Busy(true,"切换数据目录…"); await DiscardPreview(); Busy(false,"目录已切换"); await Refresh();
            }
        }
        private async Task<bool> ConfirmOwners() {
            var result = await Worker(Request("owners")); var active = Rows(result,"owners").ToList();
            if (active.Count == 0) return true;
            string names = String.Join("、", active.Select(r => S(r,"label").Length>0?S(r,"label"):S(r,"name")).Distinct());
            string text = "检测到：" + names + "\n\n请先保存草稿和编辑器文件。" + (autoClose.IsChecked == true ? "工具将请求这些软件正常退出；不会强制结束进程。" : "自动退出已关闭，请自行退出上述软件。") + "\n如果仍有后台进程，操作会安全停止。继续吗？";
            return MessageBox.Show(this,text,"保存工作后继续",MessageBoxButton.OKCancel,MessageBoxImage.Information) == MessageBoxResult.OK;
        }
        private async Task Export() {
            if (busy) return; Busy(true,"准备导出…");
            try {
                if (!await ConfirmOwners()) { Busy(false,"已取消导出"); return; }
                await PrepareBrowsing();
                var req = Request("export"); req["home"] = home; req["auto_close"] = autoClose.IsChecked == true;
                var result = await Worker(req); Busy(false,"导出完成 · " + S(result,"path"));
                MessageBox.Show(this, String.Format("已导出 {0} 条聊天，包含项目、归档和分支历史。\n\n备份包：{1}\n\n将这个 .czr 包和完整工具文件夹一起带到新电脑。备份包没有加密，请妥善保管。",N(result,"chats"),S(result,"path")),"导出成功",MessageBoxButton.OK,MessageBoxImage.Information);
                Process.Start("explorer.exe", "/select,\"" + S(result,"path") + "\"");
            } catch(Exception ex) { Error(ex); }
        }
        private async Task Preview() {
            if (busy) return;
            var dialog = new OpenFileDialog { Title="选择 codex智入 离线备份", Filter="codex智入 备份 (*.czr)|*.czr|ZIP 备份 (*.zip)|*.zip", CheckFileExists=true };
            if (dialog.ShowDialog(this) != true) return;
            Busy(true,"准备核对备份…");
            try {
                await DiscardPreview(false);
                if (!await ConfirmOwners()) { Busy(false,"已取消核对"); return; }
                await PrepareBrowsing();
                var req = Request("preview"); req["home"] = home; req["bundle"] = dialog.FileName; req["auto_close"] = autoClose.IsChecked == true;
                var result = await Worker(req); token = S(result,"token"); jobs.Add(token); archiveHome = S(result,"archive_home");
                changes = Rows(result,"changes").Select(r => new Change { Id=S(r,"id"),Title=S(r,"title"),Project=S(r,"project"),Reason=S(r,"reason"),Selectable=B(r,"selectable"),Selected=B(r,"selectable"),Helper=B(r,"helper"),Status=S(r,"status"),Source=r }).ToList();
                foreach(Change c in changes) c.PropertyChanged += delegate { UpdateSelection(); };
                RenderDifferences(); tabs.SelectedIndex = 1;
                diffSummary.Text = String.Format("备份 {0} 条 / 本机 {1} 条。可安全更新 {2} 条；相同跳过 {3} 条，本机较新跳过 {4} 条。冲突和共享历史受阻项不会覆盖。",N(result,"source_chats"),N(result,"local_chats"),N(result,"safe"),N(result,"same"),N(result,"local_newer"));
                if(result.ContainsKey("schema")) {
                    var schema=(Dictionary<string,object>)result["schema"];
                    if(!B(schema,"supported")) diffSummary.Text += "\n"+S(schema,"reason");
                    else if(Rows(schema,"differences").Any()) diffSummary.Text += "\n已适配历史数据库的新旧字段差异；较新元数据另存保留，不更改本机数据库结构。";
                }
                if (B(result,"new_profile")) diffSummary.Text += " 新目录尚未初始化，导入后需自行启动 Codex 并登录。";
                Busy(false,"核对完成，尚未写入任何本机聊天");
                compareChange=changes.FirstOrDefault(c=>!c.Helper && c.Selectable)??changes.FirstOrDefault(c=>!c.Helper)??changes.FirstOrDefault();
                if(compareChange!=null) await LoadComparison(-1);
            } catch(Exception ex) { Error(ex); }
        }
        private async Task Apply() {
            if (busy || token == null) return;
            string[] selected = changes.Where(c => c.Selected && c.Selectable).Select(c => c.Id).ToArray();
            if (selected.Length == 0) return;
            if (MessageBox.Show(this,"将安全导入 " + selected.Length + " 条聊天。\n本机较新的内容和未勾选的聊天保留，登录配置不替换。\n写入前会保留可回滚副本。继续吗？","确认导入",MessageBoxButton.OKCancel,MessageBoxImage.Question) != MessageBoxResult.OK) return;
            Busy(true,"准备导入所选聊天…");
            try {
                if (!await ConfirmOwners()) { Busy(false,"已取消导入"); return; }
                await PrepareBrowsing();
                var req = Request("apply"); req["token"] = token; req["selected"] = selected; req["auto_close"] = autoClose.IsChecked == true;
                string appliedToken=token; var result = await Worker(req); jobs.Remove(appliedToken); token = null; await DiscardPreview(false);
                Busy(false,"导入成功，现在可以重新打开 Codex");
                string compatibility=N(result,"compatibility_rows_preserved")>0?"\n旧版不支持的元数据已另存保留；以后使用新版导入时可恢复。":"";
                MessageBox.Show(this,String.Format("新增 {0} 条，更新 {1} 条，本机共 {2} 条。\n同名附件已分别保留 {4} 个。{5}\n\n回滚副本：{3}\n\n现在可以重新打开 Codex。",N(result,"added"),N(result,"updated"),N(result,"total"),S(result,"backup"),Rows(result,"renamed_assets").Count(),compatibility),"导入成功",MessageBoxButton.OK,MessageBoxImage.Information);
                await Refresh();
            } catch(Exception ex) { Error(ex); }
        }
        private async Task Rollback() {
            if(busy || MessageBox.Show(this,"恢复最近一次由 codex智入 导入前的聊天状态？\n导入后的状态也会保存在回滚目录中。登录配置不受影响。","确认回滚",MessageBoxButton.OKCancel,MessageBoxImage.Question) != MessageBoxResult.OK) return;
            Busy(true,"准备回滚…");
            try {
                if (!await ConfirmOwners()) { Busy(false,"已取消回滚"); return; }
                await PrepareBrowsing();
                var req=Request("rollback"); req["home"]=home; req["auto_close"]=autoClose.IsChecked==true;
                await Worker(req); await DiscardPreview(); Busy(false,"已恢复到最近一次导入前"); MessageBox.Show(this,"回滚完成，可以重新打开 Codex。","完成"); await Refresh();
            } catch(Exception ex) { Error(ex); }
        }
        private async Task DiscardPreview(bool resetConversation=true) {
            comparisonRequest++;
            string previous=token; token=null; archiveHome=null; changes=new List<Change>(); differences.Items.Clear(); compareChange=null; compareMessages.Children.Clear(); compareTitle.Text="点选一条聊天查看对话"; compareMeta.Text="左边勾选要导入的聊天，右边直接阅读内容。"; comparePage.Text=""; UpdateSelection();
            changeChecks.Clear();
            diffSummary.Text="点击“一键导入”，选择 .czr 备份包重新核对。相同或本机更新的聊天会跳过。";
            if(resetConversation) {
            conversationRequest++;
            viewHome=null; viewId=null; messageStart=0; messageTotal=0; messages.Children.Clear(); pageLabel.Text="";
            chatTitle.Text="选择一条聊天，继续读它的故事"; chatMeta.Text="项目、归档和分支聊天会一起显示在左侧。";
            }
            if(previous!=null) { try { var req=Request("cleanup"); req["token"]=previous; await Worker(req); jobs.Remove(previous); } catch { } }
        }
        private void Smoke(string[] args) {
            ShowInTaskbar=false; Left=-3000; Top=-3000;
            home="C:\\Users\\示例用户\\.codex"; homeLabel.Text=home; initializing=true;
            profiles.Items.Add(new Profile {Path=home,Label="当前 Codex 数据（离线演示）"}); profiles.SelectedIndex=0; initializing=false;
            chats=new List<Dictionary<string,object>> {
                new Dictionary<string,object>{{"id","a"},{"title","hello"},{"project","机器人项目"},{"project_id","p"},{"parent_id",""},{"archived",false}},
                new Dictionary<string,object>{{"id","b"},{"title","hello · 控制器分支"},{"project","机器人项目"},{"project_id","p"},{"parent_id","a"},{"archived",false}},
                new Dictionary<string,object>{{"id","c"},{"title","中文聊天名称正常显示"},{"project","未分组聊天"},{"project_id",""},{"parent_id",""},{"archived",true}}
            };
            stats.Text="3 条聊天 · 1 个项目 · 1 条归档"; RenderTree(); chatTitle.Text="hello"; chatMeta.Text="本机 · 机器人项目 · 只读预览";
            ShowMessages(new [] { new Dictionary<string,object>{{"role","user"},{"time","2026-10-15 09:30"},{"text","帮我核对保存结果，代码和重点要清楚显示。"}},new Dictionary<string,object>{{"role","assistant"},{"time","2026-10-15 09:31"},{"text",DemoMarkdown}} });
            if(tree.Items.Count>0) {var first=(TreeViewItem)tree.Items[0]; if(first.Items.Count>0) ((TreeViewItem)first.Items[0]).IsSelected=true;}
            pageLabel.Text="1–2 / 2 条";
            changes=new List<Change>{new Change{Title="hello · 控制器分支",Project="机器人项目",Reason="新增聊天",Selectable=true,Selected=true},new Change{Title="中文聊天名称正常显示",Project="未分组聊天",Reason="备份有新增对话",Selectable=true,Selected=true},new Change{Title="另一条独立续写的聊天",Project="机器人项目",Reason="双方历史不同，保留本机",Selectable=false,Selected=false}};
            RenderDifferences(); diffSummary.Text="备份 6 条 / 本机 4 条。可导入 2 条；相同跳过 2 条，本机较新跳过 1 条。较新的 hello 不在待更新列表中。";
            compareTitle.Text="hello · 控制器分支"; compareMeta.Text="机器人项目 · 新增聊天 · 备份中的对话";
            ShowMessages(new [] {new Dictionary<string,object>{{"role","assistant"},{"time","2026-10-15"},{"text","这条分支的历史会**完整导入**。\n\n```text\n原聊天：保留\n新增对话：导入\n同名附件：分别保存\n```\n\n可以切换到“这台电脑的对话”，核对更新之前的内容。"}}},compareMessages);
            selectionSummary.Text="已选 2 / 2 条可导入记录。灰色冲突项仅供核对。";
            if(args[0].Contains("import")) tabs.SelectedIndex=1;
            token="demo"; foreach(Change change in changes) change.PropertyChanged+=delegate {UpdateSelection();}; UpdateSelection();
            if(args[0].Contains("cancel")) {
                selectNone.RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
                if(changes.Any(c=>c.Selected) || applyButton.IsEnabled || selectNone.IsEnabled) throw new InvalidOperationException("Cancel-selection UI check failed");
            }
            if(args[0].Contains("empty")) { changes.Clear(); RenderDifferences(); compareMessages.Children.Clear(); compareTitle.Text="没有需要导入的变更"; compareMeta.Text="本机聊天已经包含备份中的这些内容。"; diffSummary.Text="所有聊天内容相同或本机更新，当前没有需要导入的记录。"; }
            if(args[0].Contains("loading")) BeginReading("正在读取所选聊天… 左侧仍可切换");
            if(args[0].Contains("operation")) {
                Busy(true,"正在后台导出 48% · 可继续切换聊天、阅读和复制");
                browseReady=true;browseHome="fixture-local";browseArchiveHome="fixture-backup";UpdateInteractive();
                progress.IsIndeterminate=false;progress.Value=48;chatMeta.Text+=" · 操作前的只读副本";
            }
            if(args[0].Contains("markdown")) messages.Children.RemoveAt(0);
            DispatcherTimer timer=new DispatcherTimer {Interval=TimeSpan.FromMilliseconds(600)};
            timer.Tick+=delegate { timer.Stop(); try { UpdateLayout(); var element=(FrameworkElement)Content; var bmp=new RenderTargetBitmap((int)element.ActualWidth,(int)element.ActualHeight,96,96,PixelFormats.Pbgra32); bmp.Render(element); var encoder=new PngBitmapEncoder(); encoder.Frames.Add(BitmapFrame.Create(bmp)); using(var stream=File.Create(args[1])) encoder.Save(stream); } finally { Close(); } }; timer.Start();
        }
    }

    public static class Program {
        public static void PrepareRuntime() {
            string root=AppDomain.CurrentDomain.BaseDirectory; string archive=Path.Combine(root,"vendor","python-3.11.9-embed-amd64.zip");
            using(var stream=File.OpenRead(archive)) using(var hash=SHA256.Create()) {
                string actual=BitConverter.ToString(hash.ComputeHash(stream)).Replace("-","").ToLowerInvariant();
                if(actual!="009d6bf7e3b2ddca3d784fa09f90fe54336d5b60f0e0f305c37f400bf83cfd3b") throw new InvalidOperationException("自带运行环境校验失败，请使用完整的离线工具包。");
            }
            string runtime=Path.Combine(root,".runtime");
            if(!File.Exists(Path.Combine(runtime,"python.exe"))) ZipFile.ExtractToDirectory(archive,runtime);
            using(var zip=ZipFile.OpenRead(archive)) foreach(string name in new[]{"python.exe","python311.dll","python311.zip"}) {
                using(var a=zip.GetEntry(name).Open()) using(var b=File.OpenRead(Path.Combine(runtime,name))) using(var hash=SHA256.Create()) {
                    string first=Convert.ToBase64String(hash.ComputeHash(a)); string second=Convert.ToBase64String(hash.ComputeHash(b));
                    if(first!=second) throw new InvalidOperationException("运行环境文件已改变，请重新解压完整工具包。");
                }
            }
        }
        [STAThread] public static int Main(string[] args) {
            try {
                if(args.Length==1 && args[0]=="--verify-runtime") { PrepareRuntime(); return 0; }
                bool created;
                using(var mutex=new System.Threading.Mutex(true,"Local\\CodexZhiru-1-Desktop",out created)) {
                    if(!created) { MessageBox.Show("codex智入 已经打开。请使用已打开的窗口，避免同时导入。","codex智入"); return 0; }
                    try { var app=new Application(); app.Run(new MainWindow(args)); return 0; }
                    finally { mutex.ReleaseMutex(); }
                }
            }
            catch(Exception ex) { if(args.Length>1 && args[0].StartsWith("--smoke")) File.WriteAllText(args[1]+".error.txt",ex.ToString(),Encoding.UTF8); else if(args.Length==0 || args[0]!="--verify-runtime") MessageBox.Show(ex.ToString(),"codex智入启动失败"); return 1; }
        }
    }
}
