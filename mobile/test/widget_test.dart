// This is a basic Flutter widget test.
//
// To perform an interaction with a widget in your test, use the WidgetTester
// utility in the flutter_test package. For example, you can send tap and scroll
// gestures. You can also use WidgetTester to find child widgets in the widget
// tree, read text, and verify that the values of widget properties are correct.

import 'package:flutter_test/flutter_test.dart';

import 'package:bartender_mobile/main.dart';

void main() {
  testWidgets('setup screen renders for an unconfigured app',
      (WidgetTester tester) async {
    await tester.pumpWidget(const BarTenderApp(initialUrl: ''));

    expect(find.text('BarTender'), findsOneWidget);
    expect(find.text('Enter your BarTender server URL to get started.'),
        findsOneWidget);
    expect(find.text('Connect'), findsOneWidget);
  });
}
