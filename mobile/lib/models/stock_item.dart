class StockItem {
  final int id;
  final String name;
  final String category;
  final num quantity;
  final String unit;
  final String notes;

  const StockItem({
    required this.id,
    required this.name,
    required this.category,
    required this.quantity,
    required this.unit,
    required this.notes,
  });

  factory StockItem.fromJson(Map<String, dynamic> json) => StockItem(
        id: json['id'] as int,
        name: json['name'] as String? ?? '',
        category: json['category'] as String? ?? '',
        quantity: json['quantity'] as num? ?? 0,
        unit: json['unit'] as String? ?? '',
        notes: json['notes'] as String? ?? '',
      );
}
